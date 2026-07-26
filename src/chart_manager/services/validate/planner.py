"""Plan validation rows from catalog targets, changes, and explicit filters."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from chart_manager.services.domain.graph import build_helm_dependency_index
from chart_manager.services.validate.catalog import build_catalog
from chart_manager.services.validate.domain.models import (
    SelectionResult,
    ValidatableChart,
    WorklistRow,
)
from chart_manager.services.validate.domain.spec import (
    MATCH_BY_BASENAME,
    ValidateSpec,
    resolve_namespace,
)


@dataclass(frozen=True)
class WorklistBuildResult:
    """Planned rows plus catalog diagnostics and composed chart targets."""

    rows: tuple[WorklistRow, ...] = ()
    spec_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    ignored_changes: tuple[Path, ...] = ()
    unmatched_changes: tuple[Path, ...] = ()
    chart_count_unvalidated: int = 0
    targets: dict[str, ValidatableChart] = field(default_factory=dict)

    @property
    def specs(self) -> dict[str, ValidateSpec]:
        """Compatibility view for callers not yet consuming targets."""
        return {name: target.spec for name, target in self.targets.items()}


def build_worklist(
    *,
    root: Path,
    changed_files: list[str] | None = None,
    all_charts: bool = False,
) -> WorklistBuildResult:
    """Build the deterministic chart/environment worklist."""
    root = root.resolve()
    catalog = build_catalog(root)
    targets = catalog.by_name()
    specs = {name: target.spec for name, target in targets.items()}

    if all_charts or changed_files is None:
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
    dependency_index = build_helm_dependency_index(root)
    for raw in changed_files:
        if not raw:
            continue
        parts = Path(raw).parts
        if parts and parts[0] == "policies":
            fanout_all = True
            continue
        if _is_validate_code_path(parts):
            fanout_all = True
            continue
        if _is_other_chart_manager_path(parts):
            continue
        if len(parts) < 2 or parts[0] != "charts":
            continue

        chart_name = parts[1]
        if len(parts) == 2:
            _add_all_envs(accumulated, specs, chart_name)
            _fanout_dependents(
                accumulated, specs, dependency_index, chart_name
            )
            continue
        chart_relative = Path(*parts[2:])
        if _is_chart_wide_trigger(chart_relative):
            _add_all_envs(accumulated, specs, chart_name)
            _fanout_dependents(
                accumulated, specs, dependency_index, chart_name
            )
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

    rows = (
        _cross_product(specs)
        if fanout_all
        else _materialize(specs, sorted(accumulated))
    )
    ordered_ignored = tuple(sorted(ignored_changes, key=Path.as_posix))
    ordered_unmatched = tuple(sorted(unmatched_changes, key=Path.as_posix))
    trigger_warnings = _trigger_coverage_warnings(
        ignored=ordered_ignored,
        unmatched=ordered_unmatched,
        specs=specs,
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


_CHART_WIDE_FILES = {"Chart.yaml", "validate-spec.yaml"}
_VALIDATE_CODE_PREFIXES = (("src", "chart_manager", "services", "validate"),)
_VALIDATE_INTEGRATIONS = {"helm.py", "kubeconform.py", "kyverno.py"}


def _is_chart_wide_trigger(chart_relative: Path) -> bool:
    parts = chart_relative.parts
    return bool(parts) and (
        parts[0] in _CHART_WIDE_FILES or parts[0] == "policies"
    )


def _envs_for_chart_file(
    spec: ValidateSpec,
    chart_relative: Path,
) -> tuple[list[str], bool]:
    path = chart_relative.as_posix()
    environments: set[str] = set()
    matched = False
    for pattern, value in spec.triggers.items():
        if not fnmatch.fnmatchcase(path, pattern):
            continue
        matched = True
        if value == MATCH_BY_BASENAME:
            if chart_relative.stem in spec.environments:
                environments.add(chart_relative.stem)
        elif isinstance(value, list):
            environments.update(
                environment
                for environment in value
                if environment in spec.environments
            )
    if not matched and spec.triggers_strict:
        return sorted(spec.environments), False
    return sorted(environments), matched


def _is_explicitly_ignored(spec: ValidateSpec, chart_relative: Path) -> bool:
    """Return whether an authored ignore pattern covers a chart file."""
    path = chart_relative.as_posix()
    return any(
        fnmatch.fnmatchcase(path, pattern)
        for pattern in spec.trigger_ignores
    )


def _trigger_coverage_warnings(
    *,
    ignored: tuple[Path, ...],
    unmatched: tuple[Path, ...],
    specs: dict[str, ValidateSpec],
) -> tuple[str, ...]:
    """Explain why changed chart files did not use an explicit trigger."""
    warnings = [
        f"changed chart file explicitly ignored by trigger_ignores: {path.as_posix()}"
        for path in ignored
    ]
    for path in unmatched:
        parts = path.parts
        chart = parts[1] if len(parts) >= 2 and parts[0] == "charts" else ""
        spec = specs.get(chart)
        behavior = (
            "triggers_strict selected all environments"
            if spec is not None and spec.triggers_strict
            else "no environments selected; add a trigger, trigger_ignores entry, "
            "or enable triggers_strict"
        )
        warnings.append(
            f"changed chart file matches no trigger: {path.as_posix()} ({behavior})"
        )
    return tuple(warnings)


def _add_all_envs(
    sink: set[tuple[str, str]],
    specs: dict[str, ValidateSpec],
    chart: str,
) -> None:
    spec = specs.get(chart)
    if spec is not None:
        sink.update((chart, environment) for environment in spec.environments)


def _fanout_dependents(
    sink: set[tuple[str, str]],
    specs: dict[str, ValidateSpec],
    dependency_index: dict[str, set[str]],
    chart: str,
) -> None:
    for dependent in dependency_index.get(chart, set()):
        _add_all_envs(sink, specs, dependent)


def _materialize(
    specs: dict[str, ValidateSpec],
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


def _cross_product(specs: dict[str, ValidateSpec]) -> tuple[WorklistRow, ...]:
    return _materialize(
        specs,
        [
            (chart, environment)
            for chart in sorted(specs)
            for environment in sorted(specs[chart].environments)
        ],
    )


def _is_validate_code_path(parts: tuple[str, ...]) -> bool:
    if any(parts[: len(prefix)] == prefix for prefix in _VALIDATE_CODE_PREFIXES):
        return True
    return (
        len(parts) >= 4
        and parts[:3] == ("src", "chart_manager", "integrations")
        and parts[3] in _VALIDATE_INTEGRATIONS
    )


def _is_other_chart_manager_path(parts: tuple[str, ...]) -> bool:
    return len(parts) >= 2 and parts[:2] == ("src", "chart_manager")
