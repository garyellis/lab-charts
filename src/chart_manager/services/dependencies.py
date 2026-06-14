from __future__ import annotations

from pathlib import Path

from chart_manager.plumbing.charts import ChartRepository
from chart_manager.plumbing.graph import DependencyResolver, PlanEntry
from chart_manager.plumbing.spec import ChartRef


class DependencyService:
    def __init__(self, root: Path) -> None:
        self.repository = ChartRepository(root)
        self.resolver = DependencyResolver(self.repository)

    def install_plan(self, chart: str, profile: str) -> list[PlanEntry]:
        return self.resolver.install_plan(chart, profile)

    def reverse_tests(self, chart: str) -> list[ChartRef]:
        return self.resolver.reverse_tests(chart)
