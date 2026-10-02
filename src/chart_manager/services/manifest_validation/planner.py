"""Plan manifest-validation rows from targets, changes, and explicit filters."""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from chart_manager.api.v1alpha1.chart_lifecycle import (
    ALL_ENVIRONMENTS,
    MATCH_BY_BASENAME,
    ManifestValidationSpec,
    TriggerValue,
)
from chart_manager.domain.chart_deps import build_helm_dependency_index
from chart_manager.domain.lifecycle_policy import LIFECYCLE_FILENAME
from chart_manager.domain.workspace import RepositoryWorkspace
from chart_manager.services.manifest_validation.catalog import build_catalog
from chart_manager.services.manifest_validation.models import (
    ManifestValidationTarget,
    SelectionResult,
    WorklistRow,
)
from chart_manager.services.manifest_validation.namespaces import resolve_namespace

# Repository-wide triggers merged UNDER each chart's authored `triggers`; an
# authored pattern with the identical spelling replaces the default, so a chart
# can narrow one (`"templates/**": [ci]`) or opt out (`"templates/**": []`).
# Every chart was repeating these, and a chart that forgot `templates/**` left
# template edits unvalidated.
#
# Rendering inputs (templates/**, files/**, values.schema.json, Chart.lock)
# default to all-environments: each env renders -- and schema-validates --
# them against its own values, so a change can pass under ci and break dev.
# values-ci.yaml only feeds ci, and tests/** (helm test hooks) is not read by
# manifest validation, so ci is enough to notice the edit.
#
# A default only counts as a match when it selects one of the chart's
# environments; otherwise the unmatchedChanges policy applies as if the
# default were absent. `values.yaml` is deliberately not defaulted because
# charts map it to different environment sets; `Chart.yaml` and
# `chart-lifecycle.yaml` are chart-wide before triggers are consulted.
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
class WorklistBuildResult:
    """Planned cases plus catalog diagnostics and composed chart targets."""

    rows: tuple[WorklistRow, ...] = ()
    spec_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    ignored_changes: tuple[Path, ...] = ()
    unmatched_changes: tuple[Path, ...] = ()
    chart_count_unvalidated: int = 0
    targets: dict[str, ManifestValidationTarget] = field(default_factory=dict)

    @property
    def specs(self) -> dict[str, ManifestValidationSpec]:
        """Project targets onto their authored manifest-validation specs."""
        return {name: target.spec for name, target in self.targets.items()}


def build_worklist(
    *,
    workspace: RepositoryWorkspace,
    changed_files: list[str] | None = None,
    skip_change_detection: bool = False,
    selected_charts: tuple[str, ...] = (),
) -> WorklistBuildResult:
    """Build the deterministic chart/environment worklist.

    ``selected_charts`` is a planning boundary, not a result filter: only
    those charts are loaded. Callers performing change-impact analysis must
    leave it empty so repository-wide dependencies and fanout remain visible.
    """
    root = workspace.root
    catalog = build_catalog(
        root,
        chart_names=selected_charts or None,
        charts_dir=workspace.charts_dir,
    )
    targets = catalog.by_name()
    specs = {name: target.spec for name, target in targets.items()}

    if skip_change_detection or changed_files is None:
        rows = _cross_product(specs)
        return WorklistBuildResult(
            rows=rows,
            spec_errors=catalog.errors,
            warnings=catalog.warnings,
            chart_count_unvalidated=catalog.chart_count_unvalidated,
            targets=targets,
        )

    fanout_all = False
    accumulated: set[tuple[str, str]] = set()
    ignored_changes: set[Path] = set()
    unmatched_changes: set[Path] = set()
    dependency_index = build_helm_dependency_index(root, charts_dir=workspace.charts_dir)
    for raw in changed_files:
        if not raw:
            continue
        parts = Path(raw).parts
        if workspace.matches_validation_fanout(Path(raw)):
            fanout_all = True
            continue
        chart_name = workspace.chart_name_from_repo_path(Path(raw))
        if chart_name is None:
            continue

        prefix_length = len(workspace.charts_dir.parts)
        if len(parts) == prefix_length + 1:
            _add_all_envs(accumulated, specs, chart_name)
            _fanout_dependents(accumulated, specs, dependency_index, chart_name)
            continue
        chart_relative = Path(*parts[prefix_length + 1 :])
        if chart_relative == Path(LIFECYCLE_FILENAME):
            # Lifecycle intent is chart-wide only when this chart has an
            # enabled validation capability. Disabled/unconfigured
            # capabilities never enter validation fanout.
            if chart_name in specs:
                _add_all_envs(accumulated, specs, chart_name)
                _fanout_dependents(accumulated, specs, dependency_index, chart_name)
            continue
        if _is_chart_wide_trigger(chart_relative):
            _add_all_envs(accumulated, specs, chart_name)
            _fanout_dependents(accumulated, specs, dependency_index, chart_name)
            continue
        _fanout_dependents(accumulated, specs, dependency_index, chart_name)
        spec = specs.get(chart_name)
        if spec is None:
            continue
        changed_path = Path(raw)
        if _is_explicitly_ignored(spec, chart_relative):
            ignored_changes.add(changed_path)
            continue
        environments, matched = _envs_for_chart_file(spec, chart_relative)
        if not matched:
            unmatched_changes.add(changed_path)
        for environment in environments:
            accumulated.add((chart_name, environment))

    rows = _cross_product(specs) if fanout_all else _materialize(specs, sorted(accumulated))
    ordered_ignored = tuple(sorted(ignored_changes, key=Path.as_posix))
    ordered_unmatched = tuple(sorted(unmatched_changes, key=Path.as_posix))
    trigger_warnings = _trigger_coverage_warnings(
        ignored=ordered_ignored,
        unmatched=ordered_unmatched,
        specs=specs,
        workspace=workspace,
    )
    return WorklistBuildResult(
        rows=rows,
        spec_errors=catalog.errors,
        warnings=(*catalog.warnings, *trigger_warnings),
        ignored_changes=ordered_ignored,
        unmatched_changes=ordered_unmatched,
        chart_count_unvalidated=catalog.chart_count_unvalidated,
        targets=targets,
    )

def select_rows(
    rows: tuple[WorklistRow, ...],
    *,
    charts: set[str],
    envs: set[str],
    available_charts: set[str] | None = None,
    available_environments: set[str] | None = None,
    ignored_changes: tuple[Path, ...] = (),
    unmatched_changes: tuple[Path, ...] = (),
    warnings: tuple[str, ...] = (),
) -> SelectionResult:
    """Apply explicit filters while retaining unmatched-request diagnostics.

    An environment is considered known when it exists in the candidate
    worklist before environment filtering. This deliberately makes a request
    for a real environment on a legitimate change-detection no-op valid.
    """
    known_charts = available_charts or {row.chart for row in rows}
    known_environments = available_environments or {row.env for row in rows}
    unmatched_charts = tuple(sorted(charts - known_charts))
    unmatched_environments = tuple(sorted(envs - known_environments))

    kept = rows
    if charts:
        kept = tuple(row for row in kept if row.chart in charts)
    if envs:
        kept = tuple(row for row in kept if row.env in envs)
    return SelectionResult(
        rows=kept,
        unmatched_charts=unmatched_charts,
        unmatched_environments=unmatched_environments,
        ignored_changes=ignored_changes,
        unmatched_changes=unmatched_changes,
        warnings=warnings,
        filtered_out=len(rows) - len(kept),
    )


_CHART_WIDE_FILES = {"Chart.yaml"}


def _is_chart_wide_trigger(chart_relative: Path) -> bool:
    parts = chart_relative.parts
    return bool(parts) and (parts[0] in _CHART_WIDE_FILES or parts[0] == "policies")


def _envs_for_chart_file(
    spec: ManifestValidationSpec,
    chart_relative: Path,
) -> tuple[list[str], bool]:
    path = chart_relative.as_posix()
    environments: set[str] = set()
    matched = False
    for pattern, value in {**DEFAULT_TRIGGERS, **spec.triggers}.items():
        if not fnmatch.fnmatchcase(path, pattern):
            continue
        if value == MATCH_BY_BASENAME:
            selected = {chart_relative.stem} & spec.environments.keys()
        elif value == ALL_ENVIRONMENTS:
            selected = set(spec.environments)
        else:
            selected = {environment for environment in value if environment in spec.environments}
        # Authored patterns match even when they select nothing; a default
        # only matches when the chart declares one of its environments.
        if pattern in spec.triggers or selected:
            matched = True
        environments.update(selected)
    if not matched and spec.unmatched_changes == "all-environments":
        return sorted(spec.environments), False
    return sorted(environments), matched


def _is_explicitly_ignored(spec: ManifestValidationSpec, chart_relative: Path) -> bool:
    """Return whether an authored ignore pattern covers a chart file."""
    path = chart_relative.as_posix()
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in spec.trigger_ignores)


def _trigger_coverage_warnings(
    *,
    ignored: tuple[Path, ...],
    unmatched: tuple[Path, ...],
    specs: dict[str, ManifestValidationSpec],
    workspace: RepositoryWorkspace,
) -> tuple[str, ...]:
    """Explain why changed chart files did not use an explicit trigger."""
    warnings = [
        f"changed chart file explicitly ignored by triggerIgnores: {path.as_posix()}"
        for path in ignored
    ]
    for path in unmatched:
        chart = workspace.chart_name_from_repo_path(path) or ""
        spec = specs.get(chart)
        behavior = (
            "unmatchedChanges=all-environments selected all environments"
            if spec is not None and spec.unmatched_changes == "all-environments"
            else "no environments selected; add a trigger or triggerIgnores entry, "
            "or set unmatchedChanges=all-environments"
        )
        warnings.append(f"changed chart file matches no trigger: {path.as_posix()} ({behavior})")
    return tuple(warnings)


def _add_all_envs(
    sink: set[tuple[str, str]],
    specs: dict[str, ManifestValidationSpec],
    chart: str,
) -> None:
    spec = specs.get(chart)
    if spec is not None:
        sink.update((chart, environment) for environment in spec.environments)


def _fanout_dependents(
    sink: set[tuple[str, str]],
    specs: dict[str, ManifestValidationSpec],
    dependency_index: dict[str, set[str]],
    chart: str,
) -> None:
    for dependent in dependency_index.get(chart, set()):
        _add_all_envs(sink, specs, dependent)


def _materialize(
    specs: dict[str, ManifestValidationSpec],
    pairs: list[tuple[str, str]],
) -> tuple[WorklistRow, ...]:
    rows: list[WorklistRow] = []
    for chart, environment in pairs:
        spec = specs.get(chart)
        if spec is None or environment not in spec.environments:
            continue
        rows.append(
            WorklistRow(
                chart=chart,
                env=environment,
                release=spec.release_name,
                namespace=resolve_namespace(spec, environment),
            )
        )
    return tuple(rows)


def _cross_product(specs: dict[str, ManifestValidationSpec]) -> tuple[WorklistRow, ...]:
    return _materialize(
        specs,
        [
            (chart, environment)
            for chart in sorted(specs)
            for environment in sorted(specs[chart].environments)
        ],
    )
