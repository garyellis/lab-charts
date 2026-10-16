"""Resolve a chart-test profile and its requirements into a dependencies-first install plan."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.api.v1alpha1.chart_lifecycle import ChartTestProfile
from chart_manager.plumbing.errors import DependencyCycleError, SpecError
from chart_manager.shared.charts.chart import Chart, load_chart
from chart_manager.shared.charts.lifecycle import require_chart_test, require_chart_test_profile


@dataclass(frozen=True)
class InstallPlanEntry:
    """One chart:profile step in an install plan, resolved to what it installs."""

    chart: Chart
    profile: str
    spec: ChartTestProfile
    namespace: str
    values: tuple[Path, ...]


def install_plan(charts_dir: Path, chart: str, profile: str) -> list[InstallPlanEntry]:
    """Return `chart:profile` after everything it requires, each chart loaded once.

    DFS post-order: requirements in declaration order. Raises `DependencyCycleError`
    on a cycle and `SpecError` for a missing values file.
    """
    charts: dict[str, Chart] = {}
    plan: list[InstallPlanEntry] = []
    planned: set[tuple[str, str]] = set()
    visiting: list[tuple[str, str]] = []

    def visit(chart_name: str, profile_name: str) -> None:
        key = (chart_name, profile_name)
        if key in planned:
            return
        if key in visiting:
            cycle = " -> ".join(f"{c}:{p}" for c, p in [*visiting, key])
            raise DependencyCycleError(f"dependency cycle detected: {cycle}")
        visiting.append(key)
        if chart_name not in charts:
            charts[chart_name] = load_chart(charts_dir / chart_name)
        loaded = charts[chart_name]
        chart_test = require_chart_test(loaded.lifecycle, chart_name=loaded.name)
        spec = require_chart_test_profile(chart_test, profile_name)
        for required in spec.requires:
            visit(required.chart, required.profile)
        visiting.pop()
        planned.add(key)
        plan.append(
            InstallPlanEntry(
                chart=loaded,
                profile=profile_name,
                spec=spec,
                namespace=spec.namespace,
                values=_values(loaded, profile_name, spec),
            )
        )

    visit(chart, profile)
    return plan


def _values(chart: Chart, profile: str, spec: ChartTestProfile) -> tuple[Path, ...]:
    """The profile's chart-relative values files; every one must exist."""
    paths = tuple(chart.path / value for value in spec.values)
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise SpecError(f"missing values file(s) for {chart.name}:{profile}: {', '.join(missing)}")
    return paths
