"""Dependency-graph resolution: install ordering and helm dependency indexing."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.errors import ChartManagerError, DependencyCycleError
from chart_manager.services.domain.charts import ChartRepository
from chart_manager.services.domain.spec import ChartRef


@dataclass(frozen=True)
class PlanEntry:
    """One chart:profile step in an install plan; `target` marks the requested chart."""

    chart: str
    profile: str
    target: bool = False


class DependencyResolver:
    """Resolve test-spec `requires` edges into ordered install plans."""

    def __init__(self, repository: ChartRepository) -> None:
        """Store the repository used to load charts during traversal."""
        self.repository = repository

    def install_plan(self, chart: str, profile: str) -> list[PlanEntry]:
        """Return dependencies-first install order for chart:profile.

        DFS post-order with cycle detection; raises DependencyCycleError.
        The requested chart always appears with target=True, even if it was
        already visited as someone else's dependency.
        """
        plan: list[PlanEntry] = []
        permanent: set[tuple[str, str]] = set()
        temporary: list[tuple[str, str]] = []

        def visit(ref: ChartRef, *, target: bool = False) -> None:
            """DFS one node; append to plan after its requires are planned."""
            key = (ref.chart, ref.profile)
            if key in permanent:
                # Already planned as a dependency; re-append so the dedupe
                # pass keeps the target-flagged entry (and its position last).
                if target:
                    plan.append(PlanEntry(ref.chart, ref.profile, target=True))
                return
            if key in temporary:
                cycle = " -> ".join(f"{c}:{p}" for c, p in [*temporary, key])
                raise DependencyCycleError(f"dependency cycle detected: {cycle}")

            temporary.append(key)
            chart_model = self.repository.get_managed(ref.chart)
            profile_model = chart_model.spec.profile(ref.profile)
            for required in profile_model.requires:
                visit(required)
            temporary.pop()
            permanent.add(key)
            plan.append(PlanEntry(ref.chart, ref.profile, target=target))

        visit(ChartRef(chart=chart, profile=profile), target=True)
        return _dedupe_keep_last_target(plan)

    def reverse_tests(self, chart: str) -> list[ChartRef]:
        """Return the chart's declared reverseTests refs."""
        return self.repository.get_managed(chart).spec.reverse_tests


def build_helm_dependency_index(root: Path) -> dict[str, set[str]]:
    """Map each chart in `<root>/charts/` to the chart names that depend on it.

    Loads ordinary ``HelmChart`` objects, so charts without a
    ``test-spec.yaml`` — including library charts — still enter the index.
    Malformed charts are skipped by this best-effort repository-wide scan;
    explicitly requested charts remain strict.
    """
    index: dict[str, set[str]] = {}
    repository = ChartRepository(root)
    for name in repository.list_names():
        try:
            chart = repository.get(name)
        except ChartManagerError:
            continue
        for dependency in chart.metadata.dependencies:
            index.setdefault(dependency.name, set()).add(chart.name)
    return index


def _dedupe_keep_last_target(entries: list[PlanEntry]) -> list[PlanEntry]:
    """Drop duplicate chart:profile entries, keeping the last occurrence.

    "Last wins" preserves the target=True re-append from install_plan, but
    note it also moves the entry to the duplicate's (later) position.
    """
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
