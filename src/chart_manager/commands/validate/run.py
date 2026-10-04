"""Run `chart validate`: one row per chart and environment."""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import get_args

from chart_manager.api.v1alpha1.chart_lifecycle import ManifestValidationSpec
from chart_manager.commands.validate.models import (
    FAILING,
    CheckName,
    CheckResult,
    RequestError,
    Row,
    ValidateOutcome,
    ValidateRequest,
)
from chart_manager.commands.validate.progress import NULL_PROGRESS, Progress
from chart_manager.commands.validate.schemas import generated
from chart_manager.commands.validate.schemas.runtime import (
    KubeconformSchemaRuntime,
    load_kubeconform_schema_runtime,
)
from chart_manager.commands.validate.schemas.store import default_schema_cache_root
from chart_manager.commands.validate.select import Selection, select, selected_row
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kubeconform import Kubeconform, ResourceResult
from chart_manager.integrations.kyverno import Kyverno, PolicyResult
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import (
    ChartManagerError,
    ChartNotFoundError,
    ExternalCommandError,
    MissingToolError,
    SpecError,
)
from chart_manager.shared.charts import dependencies
from chart_manager.shared.charts.chart import Chart, load_chart
from chart_manager.shared.charts.lifecycle import require_validation
from chart_manager.shared.workspace import RepositoryWorkspace


def run(
    request: ValidateRequest,
    *,
    workspace: RepositoryWorkspace,
    runner: CommandRunner,
    progress: Progress = NULL_PROGRESS,
) -> ValidateOutcome:
    """Render each requested chart in each environment, then run its validation checks.

    - An unknown chart or environment in the request raises before any work.
    - A missing run-wide prerequisite (tool binary, schema lock or cache) raises.
    - A chart's configuration error goes to `spec_errors`, and its row is left out.
    - A failed check is recorded on its row, and the row's later checks are skipped.

    Rows are checked on `request.workers` threads (0: min(cpu, 8), at least 2), or one at
    a time with `verbose` or `fail_fast`; with `fail_fast` the rows after a failed one
    are skipped.
    """
    selection = _selection(request, workspace)
    specs = {
        name: require_validation(chart.lifecycle, chart_name=name)
        for name, chart in selection.charts.items()
    }
    declared = {env for spec in specs.values() for env in spec.environments}
    unknown = sorted(set(request.envs) - declared)
    if unknown:
        raise RequestError(
            f"unknown environment(s): {', '.join(unknown)}; "
            f"declared: {', '.join(sorted(declared))}",
            flag="--env",
        )
    out = request.out
    rows = [row for row in selection.rows if not request.envs or row.env in request.envs]
    checker = _Checker(request, workspace, runner, progress)

    def check(row: Row) -> Row | str:
        try:
            checks = checker.check(
                selection.charts[row.chart], specs[row.chart], row, out / row.chart / row.env
            )
        except SpecError as exc:
            return f"{row.chart}: {exc}"
        return replace(row, checks=checks)

    progress.start(rows)
    try:
        if request.verbose or request.fail_fast:
            results = _one_at_a_time(rows, check, checker, fail_fast=request.fail_fast)
        else:
            workers = request.workers if request.workers > 0 else _auto_workers()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(check, rows))
    finally:
        progress.stop()
    return ValidateOutcome(
        rows=tuple(result for result in results if isinstance(result, Row)),
        spec_errors=(
            *selection.spec_errors,
            *(result for result in results if isinstance(result, str)),
        ),
        warnings=selection.warnings,
    )


def _one_at_a_time(
    rows: list[Row], check: Callable[[Row], Row | str], checker: _Checker, *, fail_fast: bool
) -> list[Row | str]:
    """Check rows in order; with `fail_fast`, skip every row after the first that fails."""
    results: list[Row | str] = []
    stopped = False
    for row in rows:
        result = checker.skip_all(row, "fail-fast") if stopped else check(row)
        results.append(result)
        statuses = {c.status for c in result.checks.values()} if isinstance(result, Row) else set()
        if fail_fast and statuses & FAILING:
            stopped = True
    return results


def _auto_workers() -> int:
    cpus = os.cpu_count()
    return 2 if cpus is None else max(2, min(cpus, 8))


def _selection(request: ValidateRequest, workspace: RepositoryWorkspace) -> Selection:
    """Each named chart in every environment, or only in those `changes` select; else `select()`."""
    if not request.charts:
        return select(request.changes, workspace=workspace)
    named = _named(request.charts, workspace)
    if request.changes is None:
        return named
    changed = select(request.changes, workspace=workspace)
    rows = tuple(row for row in changed.rows if row.chart in named.charts)
    return replace(named, rows=rows, warnings=changed.warnings)


def _named(names: tuple[str, ...], workspace: RepositoryWorkspace) -> Selection:
    """Every environment of each named chart.

    A chart that does not exist or has no validation raises; a malformed one is collected.
    """
    charts: dict[str, Chart] = {}
    rows: list[Row] = []
    errors: list[str] = []
    for name in names:
        try:
            chart = load_chart(workspace.chart_path(name))
        except ChartNotFoundError as exc:
            raise RequestError(str(exc), flag="--chart") from exc
        except SpecError as exc:
            errors.append(f"{name}: {exc}")
            continue
        spec = require_validation(chart.lifecycle, chart_name=chart.name)
        charts[name] = chart
        rows += [selected_row(name, spec, env) for env in spec.environments]
    return Selection(rows=tuple(rows), charts=charts, spec_errors=tuple(errors))


class _Checker:
    """Runs the requested validation checks on one row; loads the schema runtime once."""

    def __init__(
        self,
        request: ValidateRequest,
        workspace: RepositoryWorkspace,
        runner: CommandRunner,
        progress: Progress,
    ) -> None:
        self.request = request
        self.workspace = workspace
        self.runner = runner
        self.progress = progress
        self.kubeconform = Kubeconform(runner, timeout=request.tool_timeout)
        self.kyverno = Kyverno(runner, timeout=request.tool_timeout)
        self.schemas: KubeconformSchemaRuntime | None = None
        self.schemas_lock = threading.Lock()

    def check(
        self, chart: Chart, spec: ManifestValidationSpec, row: Row, rendered: Path
    ) -> dict[CheckName, CheckResult]:
        helm = _helm(
            self.runner, spec, verbose=self.request.verbose, timeout=self.request.tool_timeout
        )
        checks: dict[CheckName, CheckResult] = {
            "render": self._timed(row, "render", lambda: _render(helm, chart, spec, row, rendered))
        }
        if "schema" in self.request.checks:
            skip = _skip_reason(spec.validators.kubeconform, checks, rendered)
            if skip:
                checks["schema"] = self._skipped(row, "schema", skip)
            else:
                schemas = self._schemas()
                locations = _schema_locations(self.workspace.root, chart.name, spec)
                checks["schema"] = self._timed(
                    row,
                    "schema",
                    lambda: _schema(self.kubeconform, schemas, spec, locations, rendered),
                )
        if "policy" in self.request.checks:
            skip = _skip_reason(spec.validators.policy, checks, rendered)
            if skip:
                checks["policy"] = self._skipped(row, "policy", skip)
            else:
                policies = _policy_paths(self.workspace, chart, spec)
                checks["policy"] = self._timed(
                    row, "policy", lambda: _policy(self.kyverno, policies, rendered)
                )
        return checks

    def skip_all(self, row: Row, reason: str) -> Row:
        """The row with every requested check skipped for `reason`."""
        names = [name for name in get_args(CheckName) if name in self.request.checks]
        return replace(row, checks={name: self._skipped(row, name, reason) for name in names})

    def _timed(self, row: Row, name: CheckName, check: Callable[[], CheckResult]) -> CheckResult:
        self.progress.on_event(row, name, "running")
        started = time.monotonic()
        result = replace(check(), elapsed_seconds=time.monotonic() - started)
        self.progress.on_event(row, name, result.status, result.elapsed_seconds)
        return result

    def _skipped(self, row: Row, name: CheckName, reason: str) -> CheckResult:
        self.progress.on_event(row, name, "skipped")
        return CheckResult("skipped", reason)

    def _schemas(self) -> KubeconformSchemaRuntime:
        """The locked schema generation plus schemas generated from CRDs, loaded on first use."""
        with self.schemas_lock:
            if self.schemas is None:
                runtime = load_kubeconform_schema_runtime(self.workspace)
                _update_dependencies(self.runner, generated.providers(self.workspace))
                crds = generated.prepare(
                    self.workspace, render=self._render_crds, cache_root=default_schema_cache_root()
                )
                self.schemas = replace(runtime, generated_schema_locations=crds)
            return self.schemas

    def _render_crds(self, charts: Sequence[Chart], out: Path) -> list[str]:
        """Render each chart in every environment with its CRDs; one line per failure."""
        failures = []
        for chart in charts:
            spec = require_validation(chart.lifecycle, chart_name=chart.name)
            helm = _helm(self.runner, spec, verbose=False, timeout=self.request.tool_timeout)
            for env in spec.environments:
                row = selected_row(chart.name, spec, env)
                try:
                    result = _render(helm, chart, spec, row, out / chart.name / env, crds=True)
                except SpecError as exc:
                    result = CheckResult("failed", str(exc))
                if result.status != "passed":
                    failures.append(f"{chart.name}/{env}: {result.detail}")
        return failures


def _update_dependencies(runner: CommandRunner, charts: Sequence[Chart]) -> None:
    """Bring stale chart dependencies up to date, eight charts at a time."""
    stale = [
        chart
        for chart in charts
        if chart.metadata.dependencies and not dependencies.deps_are_fresh(chart.path)
    ]

    def update(chart: Chart) -> None:
        spec = require_validation(chart.lifecycle, chart_name=chart.name)
        _helm(runner, spec, verbose=False, timeout=None).dependency_update_if_stale(
            chart.path, timeout=300.0
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, stale))


def _helm(
    runner: CommandRunner, spec: ManifestValidationSpec, *, verbose: bool, timeout: float | None
) -> Helm:
    return Helm(
        runner,
        version=spec.helm_version,
        binary=spec.helm_binary,
        verbose=verbose,
        timeout=timeout,
        deps_are_fresh=dependencies.deps_are_fresh,
        chart_has_dependencies=dependencies.chart_has_dependencies,
    )


def _render(
    helm: Helm,
    chart: Chart,
    spec: ManifestValidationSpec,
    row: Row,
    out: Path,
    *,
    crds: bool = False,
) -> CheckResult:
    values = _values(chart, spec, row.env)
    if out.is_symlink() or out.parent.is_symlink():
        raise SpecError(f"render directory must not be a symlink: {out}")
    if out.exists():
        shutil.rmtree(out)
    try:
        helm.template(
            row.release,
            chart.path,
            namespace=row.namespace,
            output_dir=out,
            values=values,
            include_crds=crds,
        )
    except MissingToolError:
        raise
    except ExternalCommandError as exc:
        rejected = exc.returncode is not None and exc.returncode > 0
        return CheckResult(status="failed" if rejected else "error", detail=str(exc))
    return CheckResult(status="passed")


def _skip_reason(
    enabled: bool, earlier: dict[CheckName, CheckResult], rendered: Path
) -> str | None:
    """Why a check on the rendered manifests can't run, or None when it can."""
    if not enabled:
        return "disabled in chart-lifecycle.yaml"
    failed = [name for name, result in earlier.items() if result.status == "failed"]
    if failed:
        return f"{failed[0]} failed"
    if not any(rendered.rglob("*.yaml")):
        return "no manifests"
    return None


def _schema(
    kubeconform: Kubeconform,
    schemas: KubeconformSchemaRuntime,
    spec: ManifestValidationSpec,
    chart_locations: list[str],
    rendered: Path,
) -> CheckResult:
    locations = schemas.locations()
    try:
        report = kubeconform.validate(
            rendered,
            kubernetes_version=schemas.lock.policy.kubernetes_version,
            schema_locations=[
                *locations.generated_schema_locations,
                *chart_locations,
                *locations.fallback_schema_locations,
            ],
            skip_kinds=[*schemas.ignored_missing_kinds(), *spec.ignore_missing_schemas],
        )
    except MissingToolError:
        raise
    except ExternalCommandError as exc:
        return CheckResult(status="error", detail=str(exc))
    if not report.has_failures():
        return CheckResult(status="passed")
    findings = "\n".join(
        f"{r.kind}/{r.name} ({r.filename}): {r.msg or ''}".rstrip(": ") for r in report.invalid()
    )
    if any(_is_schema_unavailable(result) for result in report.errors()):
        return CheckResult(status="error", detail=f"{findings}\n{_SCHEMA_UNAVAILABLE}")
    return CheckResult(status="failed", detail=findings)


_SCHEMA_UNAVAILABLE = (
    "Schema unavailable or unreadable: check schema JSON and spec.validation.schemaLocations "
    "in the chart's chart-lifecycle.yaml. Run `chart-manager schemas sync` for missing "
    "upstream snapshots; if the kind is absent from those pins, check the current CRD "
    "providers or add a chart-local schema."
)


def _is_schema_unavailable(result: ResourceResult) -> bool:
    """A kubeconform error about loading a schema, not about the resource itself."""
    return result.msg is None or not result.msg.lower().startswith(
        ("error unmarshalling resource:", "error while parsing:", "prohibited resource kind ")
    )


def _schema_locations(root: Path, chart: str, spec: ManifestValidationSpec) -> list[str]:
    """The chart's own schema templates under ``root``; each one's directory must exist."""
    for location in spec.schema_locations:
        static = location[: location.index("{{")]
        base = root / (static if static.endswith("/") else Path(static).parent)
        if not base.is_dir():
            raise SpecError(f"chart {chart!r}: schemaLocations directory not found: {base}")
    return [str(root / location) for location in spec.schema_locations]


def _policy(kyverno: Kyverno, policies: list[Path], rendered: Path) -> CheckResult:
    if not policies:
        return CheckResult(status="skipped", detail="no policies discovered")
    try:
        report = kyverno.apply(rendered, policy_paths=policies)
    except MissingToolError:
        raise
    except ChartManagerError as exc:
        return CheckResult(status="error", detail=str(exc))
    parts = [_policy_findings(report.failures())]
    if report.warnings():
        parts.append("warnings:\n" + _policy_findings(report.warnings()))
    detail = "\n\n".join(part for part in parts if part)
    return CheckResult(status="failed" if report.has_failures() else "passed", detail=detail)


def _policy_findings(results: tuple[PolicyResult, ...]) -> str:
    return "\n".join(
        f"{r.policy}/{r.rule}: {r.resource_kind}/{r.resource_name}: {r.message or ''}".rstrip(": ")
        for r in results
    )


def _policy_paths(
    workspace: RepositoryWorkspace, chart: Chart, spec: ManifestValidationSpec
) -> list[Path]:
    """The repository and chart `policies/` directories that exist, then the chart's extras."""
    found = [path for path in (workspace.policies_root, chart.path / "policies") if path.is_dir()]
    for extra in spec.policies.extra:
        path = chart.path / extra
        if not path.is_dir():
            raise SpecError(f"chart {chart.name!r}: policies.extra is not a directory: {path}")
        if path not in found:
            found.append(path)
    return found


def _values(chart: Chart, spec: ManifestValidationSpec, env: str) -> list[Path]:
    """The environment's values files; each must exist."""
    if env not in spec.environments:
        raise SpecError(
            f"chart {chart.name!r} has no validation environment {env!r}; "
            f"declared: {', '.join(spec.environments)}"
        )
    values = [chart.path / value for value in spec.environments[env].values]
    missing = [str(path) for path in values if not path.is_file()]
    if missing:
        raise SpecError(
            f"chart {chart.name!r} env {env!r}: values file not found: {', '.join(missing)}"
        )
    return values
