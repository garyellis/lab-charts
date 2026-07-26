"""Compatibility facade for validation catalog, planning, and compilation.

New code should import the focused modules directly:

* :mod:`chart_manager.services.validate.catalog`
* :mod:`chart_manager.services.validate.planner`
* :mod:`chart_manager.services.validate.compiler`
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.errors import ChartNotFoundError
from chart_manager.services.domain.charts import ChartRepository
from chart_manager.services.validate.catalog import (
    CatalogEntry,
)
from chart_manager.services.validate.catalog import (
    discover_validate_spec as _discover_validate_spec,
)
from chart_manager.services.validate.catalog import (
    load_chart_specs as _load_chart_specs,
)
from chart_manager.services.validate.compiler import (
    compile_validate_spec,
)
from chart_manager.services.validate.compiler import (
    discover_policies as _discover_policies,
)
from chart_manager.services.validate.compiler import (
    row_config_for as _compiled_row_config_for,
)
from chart_manager.services.validate.domain.models import (
    ValidatableChart,
    WorklistRow,
)
from chart_manager.services.validate.domain.spec import ValidateSpec
from chart_manager.services.validate.planner import (
    WorklistBuildResult,
    build_worklist,
    select_rows,
)
from chart_manager.services.validate.runner import RowConfig

__all__ = [
    "LoadedSpec",
    "WorklistBuildResult",
    "apply_filters",
    "build_single_row",
    "build_worklist",
    "discover_policies",
    "discover_validate_spec",
    "load_chart_specs",
    "resolve_chart_path",
    "resolve_values",
    "row_config_for",
]


def build_single_row(*, chart: str, env: str, namespace: str, release: str) -> WorklistRow:
    """Build the explicit single-row request."""
    return WorklistRow(chart=chart, env=env, release=release, namespace=namespace)


def discover_policies(root: Path, chart: str) -> list[Path]:
    """Compatibility wrapper around compiled policy discovery."""
    return _discover_policies(root, root / "charts" / chart)


def discover_validate_spec(root: Path, chart: str) -> Path | None:
    """Compatibility wrapper around chart-path-based spec discovery."""
    return _discover_validate_spec(root / "charts" / chart)


@dataclass(frozen=True)
class LoadedSpec:
    """Legacy projection of a catalog load result."""

    chart: str
    spec: ValidateSpec | None = None
    error: str | None = None
    missing: bool = False


def load_chart_specs(root: Path, charts: Iterable[str]) -> list[LoadedSpec]:
    """Load composed targets and project them onto the legacy result shape."""
    entries: list[CatalogEntry] = _load_chart_specs(root, charts)
    return [
        LoadedSpec(
            chart=entry.chart,
            spec=entry.target.spec if entry.target is not None else None,
            error=entry.error,
            missing=entry.missing,
        )
        for entry in entries
    ]


def row_config_for(repo_root: Path, row: WorklistRow, spec: ValidateSpec) -> RowConfig:
    """Compile a legacy authored spec and build one runner configuration."""
    chart = ChartRepository(repo_root).get(row.chart)
    target = ValidatableChart(
        chart=chart,
        spec=spec,
        spec_path=chart.path / "validate-spec.yaml",
    )
    return _compiled_row_config_for(compile_validate_spec(target, repo_root), row)


def resolve_chart_path(repo_root: Path, chart: str) -> tuple[Path, str]:
    """Resolve a chart name or an explicit fixture path."""
    if "/" in chart:
        candidate = Path(chart)
        if not candidate.is_absolute():
            candidate = (repo_root / candidate).resolve()
        if not (candidate / "Chart.yaml").is_file():
            raise ChartNotFoundError(f"no Chart.yaml at {candidate}")
        return candidate, candidate.name
    resolved = ChartRepository(repo_root).get(chart)
    return resolved.path, resolved.name


def resolve_values(chart_path: Path, values: Sequence[Path]) -> list[Path]:
    """Resolve explicit values or use the chart's default values file."""
    if values:
        return [
            value if value.is_absolute() else (chart_path / value).resolve()
            for value in values
        ]
    default = chart_path / "values.yaml"
    return [default.resolve()] if default.is_file() else []


def apply_filters(
    rows: tuple[WorklistRow, ...],
    *,
    charts: set[str],
    envs: set[str],
) -> tuple[tuple[WorklistRow, ...], int]:
    """Legacy filter projection; new callers should retain ``SelectionResult``."""
    selection = select_rows(rows, charts=charts, envs=envs)
    return selection.rows, selection.filtered_out
