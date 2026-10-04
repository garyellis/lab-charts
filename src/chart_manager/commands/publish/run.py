"""Run `chart publish`: package every chart, then push each prepared archive."""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from dataclasses import replace
from functools import partial
from pathlib import Path

from chart_manager.commands.publish.models import (
    PublishedChart,
    PublishKind,
    PublishOutcome,
    PublishRequest,
    PublishTelemetryFailure,
)
from chart_manager.integrations.helm import Helm, PackageResult
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.plumbing.semver import SemVer, parse_semver
from chart_manager.services.events.failure import emit_non_fatal
from chart_manager.services.events.lifecycle import BuildPhase
from chart_manager.services.events.writer import EventWriter
from chart_manager.shared.charts import dependencies
from chart_manager.shared.charts.chart import ChartRepository
from chart_manager.shared.settings import Settings
from chart_manager.shared.workspace import RepositoryWorkspace

#: Pushes cannot be rolled back, so this channel exists to answer "which
#: artifacts actually reached the registry?" for a batch that half-succeeded.
_LOG = logging.getLogger(__name__)

# Stricter than SemVer: no identifier may start or end with a hyphen. The
# lookahead rejects numeric identifiers with leading zeros, as SemVer does.
_SUFFIX_IDENTIFIER = r"(?!0[0-9]+(?:\.|$))[0-9A-Za-z](?:[0-9A-Za-z-]*[0-9A-Za-z])?"
_SUFFIX = re.compile(rf"^{_SUFFIX_IDENTIFIER}(?:\.{_SUFFIX_IDENTIFIER})*$")


def run(
    request: PublishRequest,
    *,
    workspace: RepositoryWorkspace,
    runner: CommandRunner,
    settings: Settings,
    events: EventWriter,
) -> PublishOutcome:
    """Prepare every chart, then push each prepared archive; one row per chart.

    Preparation (unknown chart, version, dependency update, package) raises, so one failure
    pushes nothing. Pushes cannot be rolled back, so a failed push is recorded in its row and
    the others continue. A dry run prepares the same way, then pushes nothing and emits no
    lifecycle event: an event would burn the idempotency key of the real publish. It still
    writes `Chart.lock` and `charts/` through `helm dependency update`.
    """
    charts = tuple(dict.fromkeys(request.charts))
    if not charts:
        raise SpecError("at least one chart name is required")
    if not request.repository.startswith("oci://"):
        raise SpecError("repository must be an OCI URL beginning with oci://")
    if request.version is not None and request.version_suffix is not None:
        raise SpecError("--version and --version-suffix are mutually exclusive")
    if request.version is not None and len(charts) != 1:
        raise SpecError("--version is only valid when publishing exactly one chart")
    if request.version is not None:
        _validate_semver(request.version, label="version")
    kind = request.kind or (
        PublishKind.PREVIEW if request.version_suffix is not None else PublishKind.RELEASE
    )
    if kind is PublishKind.RELEASE and request.version_suffix is not None:
        raise SpecError("release publishing cannot use --version-suffix")

    # `operation_id` joins this line to the lifecycle event's `detail` in the events store.
    _LOG.info(
        "publish started: charts=%s repository=%s kind=%s version=%s "
        "version_suffix=%s dry_run=%s operation_id=%s",
        ",".join(charts),
        request.repository,
        kind.value,
        request.version or "(chart version)",
        request.version_suffix or "(none)",
        request.dry_run,
        request.operation_id or "(none)",
    )
    helm = Helm(
        runner,
        verbose=False,
        context=settings.kube_context,
        deps_are_fresh=dependencies.deps_are_fresh,
        chart_has_dependencies=dependencies.chart_has_dependencies,
    )
    repository = ChartRepository(workspace.root, charts_dir=workspace.spec.charts_dir)
    with tempfile.TemporaryDirectory(prefix="chart-manager-publish-") as work:
        prepared = [_prepare(name, request, repository, helm, Path(work)) for name in charts]
        rows = tuple(
            row if request.dry_run else _push(row, package, request, helm)
            for row, package in prepared
        )

    if request.dry_run:
        _LOG.info(
            "publish finished (dry run, nothing pushed): charts=%d kind=%s repository=%s",
            len(rows),
            kind.value,
            request.repository,
        )
        return PublishOutcome(rows, kind)
    failures = _emit_events(rows, request, kind, events)
    # Event failures are not counted here: `emit_non_fatal` already logs each one.
    _LOG.info(
        "publish finished: charts=%d pushed=%d failed=%d kind=%s repository=%s",
        len(rows),
        sum(1 for row in rows if row.ok),
        sum(1 for row in rows if not row.ok),
        kind.value,
        request.repository,
    )
    return PublishOutcome(rows, kind, failures)


def _prepare(
    name: str, request: PublishRequest, repository: ChartRepository, helm: Helm, output: Path
) -> tuple[PublishedChart, PackageResult]:
    """Package one chart at its target version; its row names the push target."""
    chart = repository.get(name)
    base_version = chart.metadata.version
    if base_version is None:
        raise SpecError(f"chart '{name}' has no version in Chart.yaml")
    version = request.version or (
        _with_version_suffix(base_version, request.version_suffix)
        if request.version_suffix is not None
        else _validate_semver(base_version, label=f"chart '{name}' version")
    )
    helm.dependency_update(chart.path)
    package = helm.package(chart.path, output, version=version if version != base_version else None)
    row = PublishedChart(name, version, _target_reference(request.repository, name, version))
    return row, package


def _push(
    row: PublishedChart, package: PackageResult, request: PublishRequest, helm: Helm
) -> PublishedChart:
    """Push one prepared archive; a failure is recorded in the row."""
    try:
        pushed = helm.push(
            package.path,
            request.repository,
            ca_file=request.ca_file,
            expected_reference=row.reference,
        )
    except ChartManagerError as exc:
        # A partially-published batch is what an operator reconciles by hand, so name it.
        _LOG.error(
            "chart push failed: chart=%s version=%s reference=%s: %s",
            row.chart,
            row.version,
            row.reference,
            exc,
        )
        return replace(row, reference=None, error=str(exc))
    return replace(row, reference=pushed.reference, digest=pushed.digest)


def _emit_events(
    rows: tuple[PublishedChart, ...],
    request: PublishRequest,
    kind: PublishKind,
    events: EventWriter,
) -> tuple[PublishTelemetryFailure, ...]:
    """Emit one retry-safe build event per successful push; return the ones that failed."""
    successful = tuple(row for row in rows if row.ok)
    phase = BuildPhase.PREVIEW_PUBLISHED if kind is PublishKind.PREVIEW else BuildPhase.PUBLISHED
    failures: list[PublishTelemetryFailure] = []
    for index, row in enumerate(successful, start=1):
        target = row.digest or row.reference or request.repository
        identity = "|".join(("build", phase.value, row.chart, row.version, target))
        detail: dict[str, object] = {
            "publish_kind": kind.value,
            "repository": request.repository,
            "reference": row.reference,
            "digest": row.digest,
            "operation_id": request.operation_id,
            "batch_index": index,
            "batch_count": len(successful),
        }

        write_event = partial(
            events.build,
            chart_name=row.chart,
            chart_version=row.version,
            phase=phase,
            build_correlation_id=request.build_correlation_id,
            pr_url=request.pr_url,
            git_sha=request.git_sha,
            detail=detail,
            idempotency_key=hashlib.sha256(identity.encode()).hexdigest(),
        )
        error = emit_non_fatal(
            write_event, strict=False, what=f"build:{phase.value} for {row.chart}@{row.version}"
        )
        if error is not None:
            failures.append(PublishTelemetryFailure(row.chart, row.version, str(error)))
    return tuple(failures)


def _target_reference(repository: str, chart: str, version: str) -> str:
    """The OCI reference a push of `chart@version` is expected to produce."""
    return f"{repository.rstrip('/')}/{chart}:{version}"


def _validate_semver(version: str, *, label: str = "version") -> str:
    """Validate strict SemVer 2.0, including numeric identifier rules."""
    _parse(version, label=label)
    return version


def _with_version_suffix(base: str, suffix: str) -> str:
    """Append prerelease identifiers while preserving existing metadata."""
    version = _parse(base, label="chart version")
    if _SUFFIX.fullmatch(suffix) is None:
        raise SpecError(f"invalid SemVer prerelease suffix: {suffix!r}")
    return str(replace(version, prerelease=version.prerelease + tuple(suffix.split("."))))


def _parse(version: str, *, label: str) -> SemVer:
    try:
        return parse_semver(version)
    except ValueError as exc:
        raise SpecError(f"invalid SemVer {label}: {version!r}") from exc
