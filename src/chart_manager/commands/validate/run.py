"""Run `chart validate`: one row per chart and environment."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from chart_manager.api.v1alpha1.chart_lifecycle import ManifestValidationSpec
from chart_manager.commands.validate.models import (
    CheckName,
    CheckResult,
    Row,
    ValidateOutcome,
    ValidateRequest,
)
from chart_manager.commands.validate.schemas import generated
from chart_manager.commands.validate.schemas.runtime import (
    KubeconformSchemaRuntime,
    load_kubeconform_schema_runtime,
)
from chart_manager.commands.validate.schemas.store import default_schema_cache_root
from chart_manager.commands.validate.select import Selection, select, selected_row
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.integrations.kyverno import Kyverno, PolicyResult
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError, SpecError
from chart_manager.shared.charts import dependencies
from chart_manager.shared.charts.chart import Chart, load_chart
from chart_manager.shared.charts.lifecycle import require_validation
from chart_manager.shared.workspace import RepositoryWorkspace


def run(
    request: ValidateRequest,
    *,
    workspace: RepositoryWorkspace,
    runner: CommandRunner,
) -> ValidateOutcome:
    """Render each requested chart in each environment, then run its validation checks.

    - An unknown chart or environment in the request raises before any work.
    - A missing run-wide prerequisite (tool binary, schema lock or cache) raises.
    - A chart's configuration error goes to `spec_errors`, and its row is left out.
    - A failed check is recorded on its row, and the row's later checks are skipped.
    """
    selection = _selection(request, workspace)
    specs = {
        name: require_validation(chart.lifecycle, chart_name=name)
        for name, chart in selection.charts.items()
    }
    declared = {env for spec in specs.values() for env in spec.environments}
    unknown = sorted(set(request.envs) - declared)
    if unknown:
        raise SpecError(
            f"unknown environment(s): {', '.join(unknown)}; declared: {', '.join(sorted(declared))}"
        )
    out = request.out or workspace.render_root / _run_id()
    checker = _Checker(request.checks, workspace, runner)
    rows: list[Row] = []
    spec_errors = list(selection.spec_errors)
    for row in selection.rows:
        if request.envs and row.env not in request.envs:
            continue
        chart = selection.charts[row.chart]
        try:
            checks = checker.check(chart, specs[row.chart], row, out / row.chart / row.env)
        except SpecError as exc:
            spec_errors.append(f"{row.chart}: {exc}")
            continue
        rows.append(replace(row, checks=checks))
    return ValidateOutcome(
        rows=tuple(rows), spec_errors=tuple(spec_errors), warnings=selection.warnings
    )


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
        except SpecError as exc:
            errors.append(f"{name}: {exc}")
            continue
        spec = require_validation(chart.lifecycle, chart_name=chart.name)
        charts[name] = chart
        rows += [selected_row(name, spec, env) for env in spec.environments]
    return Selection(rows=tuple(rows), charts=charts, spec_errors=tuple(errors))


class _Checker:
    """Runs the requested validation checks on one row; holds the schema lock once loaded."""

    def __init__(
        self, checks: frozenset[CheckName], workspace: RepositoryWorkspace, runner: CommandRunner
    ) -> None:
        self.checks = checks
        self.workspace = workspace
        self.runner = runner
        self.kubeconform = Kubeconform(runner)
        self.kyverno = Kyverno(runner)
        self.schemas: KubeconformSchemaRuntime | None = None

    def check(
        self, chart: Chart, spec: ManifestValidationSpec, row: Row, rendered: Path
    ) -> dict[CheckName, CheckResult]:
        render = _render(_helm(self.runner, spec), chart, spec, row, rendered)
        checks: dict[CheckName, CheckResult] = {"render": render}
        if "schema" in self.checks:
            skip = _skip_reason(spec.validators.kubeconform, checks, rendered)
            if skip:
                checks["schema"] = CheckResult("skipped", skip)
            else:
                if self.schemas is None:
                    runtime = load_kubeconform_schema_runtime(self.workspace)
                    _update_dependencies(self.runner, generated.providers(self.workspace))
                    crds = generated.prepare(
                        self.workspace,
                        render=self._render_crds,
                        cache_root=default_schema_cache_root(),
                    )
                    self.schemas = replace(runtime, generated_schema_locations=crds)
                locations = _schema_locations(self.workspace.root, chart.name, spec)
                checks["schema"] = _schema(
                    self.kubeconform, self.schemas, spec, locations, rendered
                )
        if "policy" in self.checks:
            skip = _skip_reason(spec.validators.policy, checks, rendered)
            if skip:
                checks["policy"] = CheckResult("skipped", skip)
            else:
                policies = _policy_paths(self.workspace, chart, spec)
                checks["policy"] = _policy(self.kyverno, policies, rendered)
        return checks

    def _render_crds(self, charts: Sequence[Chart], out: Path) -> list[str]:
        """Render each chart in every environment with its CRDs; one line per failure."""
        failures = []
        for chart in charts:
            spec = require_validation(chart.lifecycle, chart_name=chart.name)
            helm = _helm(self.runner, spec)
            for env in spec.environments:
                row = selected_row(chart.name, spec, env)
                try:
                    result = _render(helm, chart, spec, row, out / chart.name / env, crds=True)
                except SpecError as exc:
                    result = CheckResult("failed", str(exc))
                if result.status == "failed":
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
        _helm(runner, spec).dependency_update_if_stale(chart.path, timeout=300.0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, stale))


def _helm(runner: CommandRunner, spec: ManifestValidationSpec) -> Helm:
    return Helm(
        runner,
        version=spec.helm_version,
        binary=spec.helm_binary,
        verbose=False,
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
    except ExternalCommandError as exc:
        return CheckResult(status="failed", detail=str(exc))
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
    except (SpecError, ExternalCommandError) as exc:
        return CheckResult(status="failed", detail=str(exc))
    if not report.has_failures():
        return CheckResult(status="passed")
    findings = [
        f"{r.kind}/{r.name} ({r.filename}): {r.msg or ''}".rstrip(": ") for r in report.invalid()
    ]
    return CheckResult(status="failed", detail="\n".join(findings))


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
    except ChartManagerError as exc:
        return CheckResult(status="failed", detail=str(exc))
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


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
