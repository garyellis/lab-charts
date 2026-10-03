"""Run `chart validate`: one row per chart and environment."""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from string import Template

from chart_manager.api.v1alpha1.chart_lifecycle import ManifestValidationSpec
from chart_manager.commands.validate.models import (
    CheckResult,
    Row,
    ValidateOutcome,
    ValidateRequest,
)
from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError, SpecError
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
    """Render every requested chart in every requested environment."""
    out = request.out or workspace.render_root / _run_id()
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
            render = _render(helm, chart, spec, env, out / chart.name / env)
            rows.append(Row(chart=chart.name, env=env, checks={"render": render}))
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
