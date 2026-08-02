"""Headless, batch-safe Helm chart publishing."""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from chart_manager.domain.charts import ChartRepository
from chart_manager.integrations.helm import Helm, PackageResult
from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.services.events.failure import emit_non_fatal
from chart_manager.services.events.lifecycle import BuildPhase
from chart_manager.services.events.writer import EventWriter
from chart_manager.settings import DEFAULT_CHARTS_DIR

#: Pushes cannot be rolled back, so this channel exists to answer "which
#: artifacts actually reached the registry?" for a batch that half-succeeded.
_LOG = logging.getLogger(__name__)

_SEMVER = re.compile(
    r"^(?P<core>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_SUFFIX_IDENTIFIER = r"[0-9A-Za-z](?:[0-9A-Za-z-]*[0-9A-Za-z])?"
_SUFFIX = re.compile(rf"^{_SUFFIX_IDENTIFIER}(?:\.{_SUFFIX_IDENTIFIER})*$")


class PublishKind(StrEnum):
    """Meaning of a published artifact in the build lifecycle."""

    PREVIEW = "preview"
    RELEASE = "release"


@dataclass(frozen=True)
class PublishedChart:
    """Result for one chart after the push phase.

    On a dry run this is the *planned* outcome: `reference` is the target
    computed by `target_reference`, `digest` is `None` because a digest only
    exists once the registry has accepted the layer, and `error` is `None`
    because no push was attempted.
    """

    chart: str
    version: str
    reference: str | None = None
    digest: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class PublishResult:
    """Consolidated batch outcome."""

    charts: tuple[PublishedChart, ...]
    telemetry_failures: tuple[PublishTelemetryFailure, ...] = ()
    #: The kind actually used, after inference from `--version-suffix`. A dry
    #: run exists partly to reveal this, so it is reported on both paths.
    publish_kind: PublishKind | None = None
    #: True when nothing was pushed and no lifecycle event was emitted.
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(chart.ok for chart in self.charts)

    @property
    def telemetry_ok(self) -> bool:
        return not self.telemetry_failures


@dataclass(frozen=True)
class PublishTelemetryFailure:
    """A lifecycle event that could not be persisted after a successful push."""

    chart: str
    version: str
    error: str


class PublishService:
    """Prepare every chart before allowing any remote mutation."""

    def __init__(
        self,
        root: Path,
        *,
        helm: Helm,
        events: EventWriter | None = None,
        charts_dir: Path = DEFAULT_CHARTS_DIR,
    ) -> None:
        self.repository = ChartRepository(root, charts_dir=charts_dir)
        self.helm = helm
        self.events = events

    def publish(
        self,
        charts: list[str] | tuple[str, ...],
        *,
        repository: str,
        version_suffix: str | None = None,
        version: str | None = None,
        ca_file: Path | None = None,
        publish_kind: PublishKind | None = None,
        build_correlation_id: str | None = None,
        pr_url: str | None = None,
        git_sha: str | None = None,
        operation_id: str | None = None,
        dry_run: bool = False,
    ) -> PublishResult:
        """Package the whole batch, then push each prepared archive.

        Preparation failures raise and therefore push nothing. Pushes cannot
        be rolled back, so remote failures are consolidated after attempting
        every prepared chart.

        `dry_run=True` runs this same method, unchanged, up to the push
        boundary: the same argument validation, the same kind inference, the
        same version resolution, the same `helm dependency update` and `helm
        package`, and the same `target_reference` call. Only two things
        differ, and both are a single branch below: `helm push` is not
        called, and `_emit_publish_events` is not reached. There is no
        parallel "planning" implementation to drift out of sync -- what a dry
        run cannot know is exactly what the push itself produces (the digest,
        and any reference the registry echoes back in place of the expected
        one).

        A dry run is not side-effect free *locally*: `helm dependency update`
        writes `Chart.lock` and `charts/` in the source tree, and packaging
        writes into a temporary directory. It performs no remote mutation and
        emits no lifecycle event, which is what makes `publish` reversible.
        """
        selected = tuple(dict.fromkeys(charts))
        if not selected:
            raise SpecError("at least one chart name is required")
        if not repository.startswith("oci://"):
            raise SpecError("repository must be an OCI URL beginning with oci://")
        if version is not None and version_suffix is not None:
            raise SpecError("--version and --version-suffix are mutually exclusive")
        if version is not None and len(selected) != 1:
            raise SpecError("--version is only valid when publishing exactly one chart")
        if version is not None:
            validate_semver(version, label="version")
        resolved_kind = publish_kind or (
            PublishKind.PREVIEW if version_suffix is not None else PublishKind.RELEASE
        )
        if resolved_kind is PublishKind.RELEASE and version_suffix is not None:
            raise SpecError("release publishing cannot use --version-suffix")

        # `operation_id` is the caller's correlation handle and reaches the
        # lifecycle event's `detail`, so it is what joins this line to the
        # events store. No token or registry credential is in scope here.
        _LOG.info(
            "publish started: charts=%s repository=%s kind=%s version=%s "
            "version_suffix=%s dry_run=%s operation_id=%s",
            ",".join(selected),
            repository,
            resolved_kind.value,
            version or "(chart version)",
            version_suffix or "(none)",
            dry_run,
            operation_id or "(none)",
        )

        with tempfile.TemporaryDirectory(prefix="chart-manager-publish-") as work:
            output_dir = Path(work)
            prepared: list[tuple[str, str, PackageResult]] = []
            for name in selected:
                chart = self.repository.get(name)
                base_version = chart.metadata.version
                if base_version is None:
                    raise SpecError(f"chart '{name}' has no version in Chart.yaml")
                target_version = version or (
                    with_version_suffix(base_version, version_suffix)
                    if version_suffix is not None
                    else validate_semver(base_version, label=f"chart '{name}' version")
                )
                self.helm.dependency_update(chart.path)
                package = self.helm.package(
                    chart.path,
                    output_dir,
                    version=target_version if target_version != base_version else None,
                )
                prepared.append((name, target_version, package))

            outcomes: list[PublishedChart] = []
            for name, target_version, package in prepared:
                reference = target_reference(repository, name, target_version)
                if dry_run:
                    outcomes.append(
                        PublishedChart(
                            chart=name,
                            version=target_version,
                            reference=reference,
                        )
                    )
                    continue
                try:
                    pushed = self.helm.push(
                        package.path,
                        repository,
                        ca_file=ca_file,
                        expected_reference=reference,
                    )
                except ChartManagerError as exc:
                    # The batch continues so every prepared chart is attempted;
                    # each failure is consolidated into the result and named
                    # here, because a partially-published batch is the state an
                    # operator has to reconcile by hand.
                    _LOG.error(
                        "chart push failed: chart=%s version=%s reference=%s: %s",
                        name,
                        target_version,
                        reference,
                        exc,
                    )
                    outcomes.append(
                        PublishedChart(
                            chart=name,
                            version=target_version,
                            error=str(exc),
                        )
                    )
                else:
                    outcomes.append(
                        PublishedChart(
                            chart=name,
                            version=target_version,
                            reference=pushed.reference,
                            digest=pushed.digest,
                        )
                    )
            if dry_run:
                # Return before the event write. A dry run that emitted a
                # build event would burn the `idempotency_key` derived below
                # in `_emit_publish_events`, making the real publish that
                # follows look like a retry of an artifact that was never
                # pushed.
                _LOG.info(
                    "publish finished (dry run, nothing pushed): charts=%d kind=%s "
                    "repository=%s",
                    len(outcomes),
                    resolved_kind.value,
                    repository,
                )
                return PublishResult(
                    tuple(outcomes),
                    publish_kind=resolved_kind,
                    dry_run=True,
                )
            telemetry_failures = self._emit_publish_events(
                outcomes,
                repository=repository,
                publish_kind=resolved_kind,
                build_correlation_id=build_correlation_id,
                pr_url=pr_url,
                git_sha=git_sha,
                operation_id=operation_id,
            )
            # Telemetry failures are not counted here: `emit_non_fatal` already
            # logs each one, and repeating the count would double-report a
            # non-fatal condition next to a fatal one.
            _LOG.info(
                "publish finished: charts=%d pushed=%d failed=%d kind=%s repository=%s",
                len(outcomes),
                sum(1 for chart in outcomes if chart.ok),
                sum(1 for chart in outcomes if not chart.ok),
                resolved_kind.value,
                repository,
            )
            return PublishResult(
                tuple(outcomes), telemetry_failures, publish_kind=resolved_kind
            )

    def _emit_publish_events(
        self,
        outcomes: list[PublishedChart],
        *,
        repository: str,
        publish_kind: PublishKind,
        build_correlation_id: str | None,
        pr_url: str | None,
        git_sha: str | None,
        operation_id: str | None,
    ) -> tuple[PublishTelemetryFailure, ...]:
        """Emit one retry-safe lifecycle transition per successful push."""
        events = self.events
        if events is None:
            return ()

        successful = tuple(chart for chart in outcomes if chart.ok)
        phase = (
            BuildPhase.PREVIEW_PUBLISHED
            if publish_kind is PublishKind.PREVIEW
            else BuildPhase.PUBLISHED
        )
        failures: list[PublishTelemetryFailure] = []
        for index, chart in enumerate(successful, start=1):
            identity = "|".join(
                (
                    "build",
                    phase.value,
                    chart.chart,
                    chart.version,
                    chart.digest or chart.reference or repository,
                )
            )
            idempotency_key = hashlib.sha256(identity.encode()).hexdigest()
            detail: dict[str, object] = {
                "publish_kind": publish_kind.value,
                "repository": repository,
                "reference": chart.reference,
                "digest": chart.digest,
                "operation_id": operation_id,
                "batch_index": index,
                "batch_count": len(successful),
            }

            def write_event(
                chart: PublishedChart = chart,
                detail: dict[str, object] = detail,
                idempotency_key: str = idempotency_key,
            ) -> None:
                events.build(
                    chart_name=chart.chart,
                    chart_version=chart.version,
                    phase=phase,
                    build_correlation_id=build_correlation_id,
                    pr_url=pr_url,
                    git_sha=git_sha,
                    detail=detail,
                    idempotency_key=idempotency_key,
                )

            error = emit_non_fatal(
                write_event,
                strict=False,
                what=f"build:{phase.value} for {chart.chart}@{chart.version}",
            )
            if error is not None:
                failures.append(
                    PublishTelemetryFailure(chart.chart, chart.version, str(error))
                )
        return tuple(failures)


def target_reference(repository: str, chart: str, version: str) -> str:
    """The OCI reference a push of `chart@version` is expected to produce.

    The single definition of the target, shared by the push path (where it is
    `helm push`'s retry-safe `expected_reference`) and the dry-run path (where
    it is the plan). Two copies of this expression is exactly how a dry run
    starts lying about where an artifact will land.
    """
    return f"{repository.rstrip('/')}/{chart}:{version}"


def validate_semver(version: str, *, label: str = "version") -> str:
    """Validate strict SemVer 2.0, including numeric identifier rules."""
    match = _SEMVER.fullmatch(version)
    if match is None or _has_leading_zero_numeric(match.group("pre")):
        raise SpecError(f"invalid SemVer {label}: {version!r}")
    return version


def with_version_suffix(base: str, suffix: str) -> str:
    """Append prerelease identifiers while preserving existing metadata."""
    match = _SEMVER.fullmatch(base)
    if match is None or _has_leading_zero_numeric(match.group("pre")):
        raise SpecError(f"invalid SemVer chart version: {base!r}")
    if not suffix or _SUFFIX.fullmatch(suffix) is None or _has_leading_zero_numeric(suffix):
        raise SpecError(f"invalid SemVer prerelease suffix: {suffix!r}")
    core = f"{match.group('core')}.{match.group('minor')}.{match.group('patch')}"
    prerelease = ".".join(part for part in (match.group("pre"), suffix) if part)
    build = f"+{match.group('build')}" if match.group("build") else ""
    return f"{core}-{prerelease}{build}"


def _has_leading_zero_numeric(value: str | None) -> bool:
    return bool(
        value
        and any(
            part.isdigit() and len(part) > 1 and part.startswith("0")
            for part in value.split(".")
        )
    )


__all__ = [
    "PublishKind",
    "PublishResult",
    "PublishService",
    "PublishTelemetryFailure",
    "PublishedChart",
    "target_reference",
    "validate_semver",
    "with_version_suffix",
]
