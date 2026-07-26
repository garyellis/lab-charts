"""DependencyService -- thin CLI facade over DependencyResolver."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.charts import ChartRepository
from chart_manager.plumbing.graph import DependencyResolver, PlanEntry
from chart_manager.plumbing.spec import ChartRef, CheckSpec


@dataclass(frozen=True)
class PlanChecks:
    """One install-plan entry paired with the checks that entry will run."""

    chart: str
    profile: str
    checks: tuple[CheckSpec, ...]


class DependencyService:
    """Expose install-plan and reverse-test resolution to the CLI."""

    def __init__(self, root: Path) -> None:
        """Build the repository and resolver from the chart repo root."""
        self.repository = ChartRepository(root)
        self.resolver = DependencyResolver(self.repository)

    def install_plan(self, chart: str, profile: str) -> list[PlanEntry]:
        """Return the dependency-ordered install plan for chart:profile."""
        return self.resolver.install_plan(chart, profile)

    def reverse_tests(self, chart: str) -> list[ChartRef]:
        """Return the reverse-test targets declared in `chart`'s test spec."""
        return self.resolver.reverse_tests(chart)

    def checks_for(self, chart: str, profile: str) -> list[CheckSpec]:
        """Return the checks one chart:profile actually runs (declared + implicit)."""
        return self.repository.get(chart).spec.profile(profile).effective_checks()

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
