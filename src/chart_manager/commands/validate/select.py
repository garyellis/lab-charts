"""Pick the rows a set of changed files calls for."""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from types import MappingProxyType

from chart_manager.api.v1alpha1.chart_lifecycle import (
    ALL_ENVIRONMENTS,
    MATCH_BY_BASENAME,
    ManifestValidationSpec,
    TriggerValue,
)
from chart_manager.commands.validate.models import Row
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.shared.charts.chart import Chart, ChartRepository, load_chart
from chart_manager.shared.charts.dependencies import build_helm_dependency_index
from chart_manager.shared.charts.lifecycle import (
    LIFECYCLE_FILENAME,
    CapabilityStatus,
    require_validation,
    validation_status,
)
from chart_manager.shared.workspace import RepositoryWorkspace

# Merged under each chart's own `triggers`; an authored pattern with the same
# spelling replaces the default. Rendering inputs touch every environment.
DEFAULT_TRIGGERS: Mapping[str, TriggerValue] = MappingProxyType(
    {
        "values-ci.yaml": ["ci"],
        "templates/**": ALL_ENVIRONMENTS,
        "tests/**": ["ci"],
        "files/**": ALL_ENVIRONMENTS,
        "values.schema.json": ALL_ENVIRONMENTS,
        "Chart.lock": ALL_ENVIRONMENTS,
    }
)


@dataclass(frozen=True)
class Selection:
    """The selected rows (no checks run yet), the charts they belong to, and load errors."""

    rows: tuple[Row, ...]
    charts: Mapping[str, Chart] = field(default_factory=dict)
    spec_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def select(changes: Sequence[str] | None, *, workspace: RepositoryWorkspace) -> Selection:
    """Rows for every chart with validation enabled; with `changes`, only those they touch.

    A changed file selects:
    - everything, when it matches the workspace's validation fanout;
    - every environment of its chart and of the charts that depend on it, when it is
      `Chart.yaml`, `chart-lifecycle.yaml`, under `policies/` or at the chart root;
    - otherwise the environments its chart's triggers (over `DEFAULT_TRIGGERS`) name,
      unless `triggerIgnores` covers it, with `unmatchedChanges` deciding the rest.
    """
    charts, spec_errors, warnings = _load(workspace)
    specs = {name: require_validation(c.lifecycle, chart_name=name) for name, c in charts.items()}
    if changes is None:
        pairs = {(chart, env) for chart, spec in specs.items() for env in spec.environments}
    else:
        pairs, change_warnings = _pairs_for(changes, specs, workspace)
        warnings += change_warnings
    rows = tuple(selected_row(chart, specs[chart], env) for chart, env in sorted(pairs))
    return Selection(
        rows=rows, charts=charts, spec_errors=tuple(spec_errors), warnings=tuple(warnings)
    )


def selected_row(chart: str, spec: ManifestValidationSpec, env: str) -> Row:
    """A row for `chart` in `env`, with its release and namespace and no checks run."""
    namespace = spec.environments[env].namespace
    if not namespace:
        assert spec.namespace_template is not None  # the API model requires one or the other
        namespace = Template(spec.namespace_template).safe_substitute(env=env)
    return Row(chart=chart, env=env, release=spec.release_name, namespace=namespace, checks={})


def _load(workspace: RepositoryWorkspace) -> tuple[dict[str, Chart], list[str], list[str]]:
    """Every chart with validation enabled; load errors and the other charts are reported."""
    charts: dict[str, Chart] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for name in ChartRepository(workspace.root, charts_dir=workspace.spec.charts_dir).list_names():
        try:
            chart = load_chart(workspace.chart_path(name))
        except ChartManagerError as exc:
            errors.append(f"{name}: {exc}")
            continue
        status = validation_status(chart.lifecycle)
        if status is CapabilityStatus.ENABLED:
            charts[name] = chart
        elif chart.lifecycle is None:
            warnings.append(
                f"chart {name} has no {LIFECYCLE_FILENAME} — skipping manifest validation"
            )
        elif status is CapabilityStatus.ABSENT:
            warnings.append(
                f"chart {name} has no validation configuration in {LIFECYCLE_FILENAME} — skipping"
            )
        else:
            warnings.append(f"manifest validation is disabled for chart {name} — skipping")
    return charts, errors, warnings


def _pairs_for(
    changes: Sequence[str],
    specs: dict[str, ManifestValidationSpec],
    workspace: RepositoryWorkspace,
) -> tuple[set[tuple[str, str]], list[str]]:
    pairs: set[tuple[str, str]] = set()
    ignored: list[str] = []
    unmatched: list[str] = []
    fanout = False
    dependents = build_helm_dependency_index(workspace.root, charts_dir=workspace.spec.charts_dir)
    prefix = len(workspace.spec.charts_dir.parts)

    def every_env(*charts: str) -> None:
        for name in charts:
            if name in specs:
                pairs.update((name, env) for env in specs[name].environments)

    for raw in sorted({raw for raw in changes if raw}):
        path = Path(raw)
        if workspace.matches_validation_fanout(path):
            fanout = True
            continue
        chart = workspace.chart_name_from_repo_path(path)
        if chart is None:
            continue
        relative = Path(*path.parts[prefix + 1 :])
        if relative == Path(LIFECYCLE_FILENAME) and chart not in specs:
            continue
        every_env(*dependents.get(chart, ()))
        if _is_chart_wide(relative):
            every_env(chart)
            continue
        spec = specs.get(chart)
        if spec is None:
            continue
        if any(fnmatch.fnmatchcase(relative.as_posix(), p) for p in spec.trigger_ignores):
            ignored.append(f"changed chart file explicitly ignored by triggerIgnores: {raw}")
            continue
        envs, matched = _triggered(spec, relative)
        if not matched:
            behavior = (
                "unmatchedChanges=all-environments selected all environments"
                if spec.unmatched_changes == "all-environments"
                else "no environments selected; add a trigger or triggerIgnores entry, "
                "or set unmatchedChanges=all-environments"
            )
            unmatched.append(f"changed chart file matches no trigger: {raw} ({behavior})")
        pairs.update((chart, env) for env in envs)
    if fanout:
        pairs = {(c, e) for c, spec in specs.items() for e in spec.environments}
    return pairs, ignored + unmatched


def _is_chart_wide(relative: Path) -> bool:
    """The chart directory itself, its Chart.yaml or lifecycle file, or anything under policies/."""
    return not relative.parts or relative.parts[0] in ("Chart.yaml", LIFECYCLE_FILENAME, "policies")


def _triggered(spec: ManifestValidationSpec, relative: Path) -> tuple[set[str], bool]:
    """The environments a chart file's triggers select, and whether any trigger matched it."""
    envs: set[str] = set()
    matched = False
    for pattern, value in {**DEFAULT_TRIGGERS, **spec.triggers}.items():
        if not fnmatch.fnmatchcase(relative.as_posix(), pattern):
            continue
        if value == MATCH_BY_BASENAME:
            selected = {relative.stem} & spec.environments.keys()
        elif value == ALL_ENVIRONMENTS:
            selected = set(spec.environments)
        else:
            selected = {env for env in value if env in spec.environments}
        # An authored pattern counts even when it selects nothing; a default only
        # when it selects one of the chart's environments.
        matched = matched or pattern in spec.triggers or bool(selected)
        envs |= selected
    if not matched and spec.unmatched_changes == "all-environments":
        return set(spec.environments), False
    return envs, matched
