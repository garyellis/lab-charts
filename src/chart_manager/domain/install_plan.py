"""Cluster-test dependency resolution."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from chart_manager.api.lifecycle.v1alpha1 import ClusterTestRef
from chart_manager.domain.charts import ClusterTestChart
from chart_manager.domain.lifecycle_policy import require_cluster_test_profile
from chart_manager.plumbing.errors import DependencyCycleError


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

        def visit(chart_name: str, profile_name: str) -> None:
            """DFS one node; append to plan after its requires are planned."""
            key = (chart_name, profile_name)
            if key in permanent:
                return
            if key in temporary:
                cycle = " -> ".join(f"{c}:{p}" for c, p in [*temporary, key])
                raise DependencyCycleError(f"dependency cycle detected: {cycle}")

            temporary.append(key)
            chart_model = self._load_chart(chart_name)
            profile_model = require_cluster_test_profile(chart_model.spec, profile_name)
            for required in profile_model.requires:
                visit(required.chart, required.profile)
            temporary.pop()
            permanent.add(key)
            plan.append(InstallPlanEntry(chart_name, profile_name))

        visit(chart, profile)
        return plan

    def dependent_tests(self, chart: str) -> list[ClusterTestRef]:
        """Return the chart's declared dependent-test targets."""
        return self._load_chart(chart).spec.dependent_tests
