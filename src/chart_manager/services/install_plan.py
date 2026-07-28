"""InstallPlanService -- thin CLI facade over DependencyResolver."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.domain.cluster_tests import ClusterCheckSpec, ClusterTestRef
from chart_manager.services.domain.install_plan import DependencyResolver, InstallPlanEntry
from chart_manager.settings import DEFAULT_CHARTS_DIR


@dataclass(frozen=True)
class PlanChecks:
    """One install-plan entry paired with the checks that entry will run."""

    chart: str
    profile: str
    checks: tuple[ClusterCheckSpec, ...]


class InstallPlanService:
    """Expose install-plan and dependent-test resolution to the CLI."""

    def __init__(self, root: Path, *, charts_dir: Path = DEFAULT_CHARTS_DIR) -> None:
        """Build the repository and resolver from the chart repo root."""
        self.catalog = ClusterTestCatalog(root, charts_dir=charts_dir)
        self.resolver = DependencyResolver(self.catalog.get)

    def install_plan(self, chart: str, profile: str) -> list[InstallPlanEntry]:
        """Return the dependency-ordered install plan for chart:profile."""
        return self.resolver.install_plan(chart, profile)

    def dependent_tests(self, chart: str) -> list[ClusterTestRef]:
        """Return the chart's declared dependent cluster-test targets."""
        return self.resolver.dependent_tests(chart)

    def checks_for(self, chart: str, profile: str) -> list[ClusterCheckSpec]:
        """Return the checks one chart:profile actually runs (declared + implicit)."""
        return self.catalog.get(chart).spec.profile(profile).effective_checks()

    def plan_checks(self, chart: str, profile: str) -> list[PlanChecks]:
        """Return the effective checks for every entry in chart:profile's install plan.

        Saves callers from walking the plan and reaching back into the
        repository for each entry -- the two-step traversal was the reason
        the CLI held a repository reference at all.
        """
        return [
            PlanChecks(
                chart=entry.chart,
                profile=entry.profile,
                checks=tuple(self.checks_for(entry.chart, entry.profile)),
            )
            for entry in self.install_plan(chart, profile)
        ]
