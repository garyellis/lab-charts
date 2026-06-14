from __future__ import annotations

from dataclasses import dataclass

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
                cycle = " -> ".join([f"{c}:{p}" for c, p in temporary + [key]])
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
