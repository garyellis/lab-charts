"""ChartService -- thin CLI facade over ChartRepository."""
from __future__ import annotations

from pathlib import Path

from chart_manager.services.domain.charts import ChartRepository, ManagedChart


class ChartService:
    """List and fetch charts from the repository."""

    def __init__(self, root: Path) -> None:
        """Build the repository from the chart repo root."""
        self.repository = ChartRepository(root)

    def list_charts(self) -> list[str]:
        """Return all chart names."""
        return self.repository.list_names()

    def get_chart(self, name: str) -> ManagedChart:
        """Return the managed chart named ``name``."""
        return self.repository.get_managed(name)
