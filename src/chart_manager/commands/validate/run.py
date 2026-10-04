"""Run `chart validate`: one row per chart and environment."""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from string import Template

from chart_manager.api.v1alpha1.chart_lifecycle import ManifestValidationSpec
from chart_manager.commands.validate.models import (
    CheckName,
    CheckResult,
    Row,
    ValidateOutcome,
    ValidateRequest,
)
from chart_manager.commands.validate.schemas.runtime import (
    KubeconformSchemaRuntime,
    load_kubeconform_schema_runtime,
)
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
    """Render every requested chart in every requested environment, then check it."""
    out = request.out or workspace.render_root / _run_id()
    schemas: KubeconformSchemaRuntime | None = None
    kubeconform = Kubeconform(runner)
    kyverno = Kyverno(runner)
    rows = []
    for name in request.charts:
        chart = load_chart(workspace.chart_path(name))
        spec = require_validation(chart.lifecycle, chart_name=chart.name)
        helm = Helm(
            runner,
            version=spec.helm_version,
            binary=spec.helm_binary,
            verbose=False,
            deps_are_fresh=dependencies.deps_are_fresh,
            chart_has_dependencies=dependencies.chart_has_dependencies,
        )
        for env in request.envs or tuple(spec.environments):
            rendered = out / chart.name / env
            render = _render(helm, chart, spec, env, rendered)
            checks: dict[CheckName, CheckResult] = {"render": render}
            if "schema" in request.checks:
                skip = _skip_reason(spec.validators.kubeconform, checks, rendered)
                if skip:
                    checks["schema"] = CheckResult("skipped", skip)
                else:
                    if schemas is None:
                        schemas = load_kubeconform_schema_runtime(workspace)
                    locations = _schema_locations(workspace.root, chart.name, spec)
                    checks["schema"] = _schema(kubeconform, schemas, spec, locations, rendered)
            if "policy" in request.checks:
                skip = _skip_reason(spec.validators.policy, checks, rendered)
                if skip:
                    checks["policy"] = CheckResult("skipped", skip)
                else:
                    policies = _policy_paths(workspace, chart, spec)
                    checks["policy"] = _policy(kyverno, policies, rendered)
            rows.append(Row(chart=chart.name, env=env, checks=checks))
    return ValidateOutcome(rows=tuple(rows))


def _render(
    helm: Helm, chart: Chart, spec: ManifestValidationSpec, env: str, out: Path
) -> CheckResult:
    values = _values(chart, spec, env)
    if out.exists():
        shutil.rmtree(out)
    try:
        helm.template(
            spec.release_name,
            chart.path,
            namespace=_namespace(spec, env),
            output_dir=out,
            values=values,
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


def _namespace(spec: ManifestValidationSpec, env: str) -> str:
    """The environment's own namespace, else `namespaceTemplate` with `${env}` filled in."""
    explicit = spec.environments[env].namespace
    if explicit:
        return explicit
    assert spec.namespace_template is not None  # the API model requires one or the other
    return Template(spec.namespace_template).safe_substitute(env=env)


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
