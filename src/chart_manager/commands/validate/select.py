"""Pick the rows a set of changed files calls for."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
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
from chart_manager.shared.charts.chart import Chart, chart_names, load_chart
from chart_manager.shared.charts.dependencies import build_helm_dependency_index
from chart_manager.shared.charts.lifecycle import (
    LIFECYCLE_FILENAME,
    CapabilityStatus,
    require_validation,
    validation_status,
)
from chart_manager.shared.workspace import (
    SCHEMA_LOCK_FILE,
    WORKSPACE_FILE,
    RepositoryWorkspace,
    pattern_matches,
)

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


class ReasonCode(StrEnum):
    """The rule that selected a row."""

    VALIDATION_TRIGGER = "validation-trigger"
    HELM_DEPENDENT = "helm-dependent"
    REPOSITORY_POLICY = "repository-policy"
    VALIDATION_ENGINE = "validation-engine"


@dataclass(frozen=True)
class Reason:
    """One changed file and the rule by which it selected a row."""

    code: ReasonCode
    changed_file: Path
    detail: str


@dataclass(frozen=True)
class Selection:
    """The selected rows (no checks run yet), the charts they belong to, and what was left out.

    `reasons` maps each (chart, env) row that changes selected to why, in changed-file order.
    """

    rows: tuple[Row, ...]
    charts: Mapping[str, Chart] = field(default_factory=dict)
    reasons: Mapping[tuple[str, str], tuple[Reason, ...]] = field(default_factory=dict)
    spec_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    ignored_changes: tuple[str, ...] = ()
    unmatched_changes: tuple[str, ...] = ()
    charts_unvalidated: int = 0


def select(changes: Sequence[str] | None, *, workspace: RepositoryWorkspace) -> Selection:
    """Rows for every chart with validation enabled; with `changes`, only those they touch.

    A changed file selects:
    - everything, when it matches the workspace's validation fanout;
    - every environment of its chart and of the charts that depend on it, when it is
      `Chart.yaml`, `chart-lifecycle.yaml`, under `policies/` or at the chart root;
    - otherwise the environments its chart's triggers (over `DEFAULT_TRIGGERS`) name,
      unless `triggerIgnores` covers it, with `unmatchedChanges` deciding the rest.
    """
    charts, spec_errors, skipped = _load(workspace)
    specs = {name: require_validation(c.lifecycle, chart_name=name) for name, c in charts.items()}
    reasons: dict[tuple[str, str], tuple[Reason, ...]] = {}
    ignored: list[str] = []
    unmatched: list[str] = []
    if changes is None:
        pairs = {(chart, env) for chart, spec in specs.items() for env in spec.environments}
        warnings = skipped
    else:
        reasons, ignored, unmatched, change_warnings = _reasons_for(changes, specs, workspace)
        pairs = set(reasons)
        warnings = skipped + change_warnings
    rows = tuple(selected_row(chart, specs[chart], env) for chart, env in sorted(pairs))
    return Selection(
        rows=rows,
        charts=charts,
        reasons=reasons,
        spec_errors=tuple(spec_errors),
        warnings=tuple(warnings),
        ignored_changes=tuple(ignored),
        unmatched_changes=tuple(unmatched),
        charts_unvalidated=len(skipped),
    )


def selected_row(chart: str, spec: ManifestValidationSpec, env: str) -> Row:
    """A row for `chart` in `env`, with its release and namespace and no checks run."""
    namespace = spec.environments[env].namespace
    if not namespace:
        assert spec.namespace_template is not None  # the API model requires one or the other
        namespace = Template(spec.namespace_template).safe_substitute(env=env)
    return Row(chart=chart, env=env, release=spec.release_name, namespace=namespace, checks={})


def _load(workspace: RepositoryWorkspace) -> tuple[dict[str, Chart], list[str], list[str]]:
    """Every chart with validation enabled; load errors, and one warning per other chart."""
    charts: dict[str, Chart] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for name in chart_names(workspace.charts_root):
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


def _reasons_for(
    changes: Sequence[str],
    specs: dict[str, ManifestValidationSpec],
    workspace: RepositoryWorkspace,
) -> tuple[dict[tuple[str, str], tuple[Reason, ...]], list[str], list[str], list[str]]:
    """Why `changes` select each (chart, env) pair; the ignored and unmatched changes; warnings."""
    by_file: dict[tuple[str, str], dict[str, Reason]] = {}
    ignored: list[str] = []
    unmatched: list[str] = []
    ignored_warnings: list[str] = []
    unmatched_warnings: list[str] = []
    fanout: dict[str, Reason] = {}
    dependents = build_helm_dependency_index(workspace.root, charts_dir=workspace.spec.charts_dir)
    prefix = len(workspace.spec.charts_dir.parts)
    marker = workspace.marker.relative_to(workspace.root)
    policies = workspace.spec.policies_dir.parts
    # Changes to these validate every configured environment.
    fanout_patterns = (
        *workspace.spec.fanout.validation,
        workspace.spec.policies_dir.as_posix(),
        SCHEMA_LOCK_FILE.as_posix(),
        WORKSPACE_FILE.as_posix(),
    )

    def select_envs(chart: str, envs: Iterable[str], raw: str, reason: Reason) -> None:
        for env in envs:
            by_file.setdefault((chart, env), {})[raw] = reason

    def every_env(chart: str, raw: str, code: ReasonCode, detail: str) -> None:
        if chart in specs:
            select_envs(chart, specs[chart].environments, raw, Reason(code, Path(raw), detail))

    for raw in sorted({raw for raw in changes if raw}):
        path = Path(raw)
        if any(pattern_matches(pattern, path) for pattern in fanout_patterns):
            if path == marker or path.parts[: len(policies)] == policies:
                code, rule = ReasonCode.REPOSITORY_POLICY, "repository policy"
            else:
                code, rule = ReasonCode.VALIDATION_ENGINE, "validation implementation"
            detail = f"{rule} changes validate every configured environment"
            fanout[raw] = Reason(code, path, detail)
            continue
        chart = workspace.chart_name_from_repo_path(path)
        if chart is None:
            continue
        relative = Path(*path.parts[prefix + 1 :])
        if relative == Path(LIFECYCLE_FILENAME) and chart not in specs:
            continue
        for dependent in dependents.get(chart, ()):
            detail = f"{dependent} declares a Helm dependency on {chart}"
            every_env(dependent, raw, ReasonCode.HELM_DEPENDENT, detail)
        triggered = f"authored validation triggers selected {chart}"
        if _is_chart_wide(relative):
            every_env(chart, raw, ReasonCode.VALIDATION_TRIGGER, triggered)
            continue
        spec = specs.get(chart)
        if spec is None:
            continue
        if any(fnmatch.fnmatchcase(relative.as_posix(), p) for p in spec.trigger_ignores):
            ignored.append(raw)
            ignored_warnings.append(
                f"changed chart file explicitly ignored by triggerIgnores: {raw}"
            )
            continue
        envs, matched = _triggered(spec, relative)
        if not matched:
            behavior = (
                "unmatchedChanges=all-environments selected all environments"
                if spec.unmatched_changes == "all-environments"
                else "no environments selected; add a trigger or triggerIgnores entry, "
                "or set unmatchedChanges=all-environments"
            )
            unmatched.append(raw)
            unmatched_warnings.append(f"changed chart file matches no trigger: {raw} ({behavior})")
        select_envs(chart, envs, raw, Reason(ReasonCode.VALIDATION_TRIGGER, path, triggered))
    for raw, reason in fanout.items():
        for name, spec in specs.items():
            select_envs(name, spec.environments, raw, reason)
    reasons = {pair: tuple(r for _, r in sorted(files.items())) for pair, files in by_file.items()}
    return reasons, ignored, unmatched, ignored_warnings + unmatched_warnings


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
