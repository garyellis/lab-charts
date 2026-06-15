from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from chart_manager.plumbing.charts import ChartRepository
from chart_manager.plumbing.errors import DependencyCycleError
from chart_manager.plumbing.spec import ChartRef


@dataclass(frozen=True)
class PlanEntry:
    chart: str
    profile: str
    target: bool = False


class DependencyResolver:
    def __init__(self, repository: ChartRepository) -> None:
        self.repository = repository

    def install_plan(self, chart: str, profile: str) -> list[PlanEntry]:
        plan: list[PlanEntry] = []
        permanent: set[tuple[str, str]] = set()
        temporary: list[tuple[str, str]] = []

        def visit(ref: ChartRef, *, target: bool = False) -> None:
            key = (ref.chart, ref.profile)
            if key in permanent:
                if target:
                    plan.append(PlanEntry(ref.chart, ref.profile, target=True))
                return
            if key in temporary:
                cycle = " -> ".join(f"{c}:{p}" for c, p in [*temporary, key])
                raise DependencyCycleError(f"dependency cycle detected: {cycle}")

            temporary.append(key)
            chart_model = self.repository.get(ref.chart)
            profile_model = chart_model.spec.profile(ref.profile)
            for required in profile_model.requires:
                visit(required)
            temporary.pop()
            permanent.add(key)
            plan.append(PlanEntry(ref.chart, ref.profile, target=target))

        visit(ChartRef(chart=chart, profile=profile), target=True)
        return _dedupe_keep_last_target(plan)

    def reverse_tests(self, chart: str) -> list[ChartRef]:
        return self.repository.get(chart).spec.reverse_tests


def build_helm_dependency_index(root: Path) -> dict[str, set[str]]:
    """Map each chart in `<root>/charts/` to the chart names that depend on it.

    Reads `Chart.yaml` directly (rather than going through `ChartRepository`)
    so charts without a `test-spec.yaml` — including any library charts —
    still enter the index. The key is the depended-on chart name as written
    in `dependencies[].name`; the value is the set of dependent chart
    directory names. Used by the validate worklist to fan a library-chart
    edit out to every dependent chart.
    """
    charts_dir = root / "charts"
    index: dict[str, set[str]] = {}
    if not charts_dir.is_dir():
        return index
    for chart_dir in charts_dir.iterdir():
        if not chart_dir.is_dir():
            continue
        chart_yaml = chart_dir / "Chart.yaml"
        if not chart_yaml.is_file():
            continue
        try:
            data = yaml.safe_load(chart_yaml.read_text()) or {}
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        deps = data.get("dependencies") or []
        if not isinstance(deps, list):
            continue
        for dep in deps:
            if not isinstance(dep, dict):
                continue
            name = dep.get("name")
            if not name:
                continue
            index.setdefault(str(name), set()).add(chart_dir.name)
    return index


def _dedupe_keep_last_target(entries: list[PlanEntry]) -> list[PlanEntry]:
    result: list[PlanEntry] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry.chart, entry.profile)
        if key in seen:
            result = [
                existing
                for existing in result
                if (existing.chart, existing.profile) != key
            ]
        seen.add(key)
        result.append(entry)
    return result
