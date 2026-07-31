"""Cluster-test dependency resolution."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from chart_manager.plumbing.errors import DependencyCycleError
from chart_manager.services.domain.charts import ClusterTestChart
from chart_manager.services.domain.cluster_tests import ClusterTestRef


@dataclass(frozen=True)
class InstallPlanEntry:
    """One chart:profile step in a dependencies-first install plan."""

    chart: str
    profile: str


ClusterTestLoader = Callable[[str], ClusterTestChart]


class DependencyResolver:
    """Resolve cluster-test requirements into ordered install plans."""

    def __init__(self, load_chart: ClusterTestLoader) -> None:
        """Store the capability loader used during traversal."""
        self._load_chart = load_chart

    def install_plan(self, chart: str, profile: str) -> list[InstallPlanEntry]:
        """Return dependencies-first install order for chart:profile.

        DFS post-order with cycle detection; raises DependencyCycleError.
        """
        plan: list[InstallPlanEntry] = []
        permanent: set[tuple[str, str]] = set()
        temporary: list[tuple[str, str]] = []

        def visit(ref: ClusterTestRef) -> None:
            """DFS one node; append to plan after its requires are planned."""
            key = (ref.chart, ref.profile)
            if key in permanent:
                return
            if key in temporary:
                cycle = " -> ".join(f"{c}:{p}" for c, p in [*temporary, key])
                raise DependencyCycleError(f"dependency cycle detected: {cycle}")

            temporary.append(key)
            chart_model = self._load_chart(ref.chart)
            profile_model = chart_model.spec.profile(ref.profile)
            for required in profile_model.requires:
                visit(required)
            temporary.pop()
            permanent.add(key)
            plan.append(InstallPlanEntry(ref.chart, ref.profile))

        visit(ClusterTestRef(chart=chart, profile=profile))
        return plan

    def dependent_tests(self, chart: str) -> list[ClusterTestRef]:
        """Return the chart's declared dependent-test targets."""
        return self._load_chart(chart).spec.dependent_tests
