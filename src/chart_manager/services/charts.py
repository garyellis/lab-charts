from __future__ import annotations

from pathlib import Path

from chart_manager.plumbing.charts import Chart, ChartRepository


class ChartService:
    def __init__(self, root: Path) -> None:
        self.repository = ChartRepository(root)

    def list_charts(self) -> list[str]:
        return self.repository.list_names()

    def get_chart(self, name: str) -> Chart:
        return self.repository.get(name)
