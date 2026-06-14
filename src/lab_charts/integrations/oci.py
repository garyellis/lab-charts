from __future__ import annotations

from lab_charts.plumbing.errors import LabChartsError


class OciRegistry:
    def chart_ref(self, chart: str, version: str | None = None) -> str:
        raise LabChartsError(
            "OCI registry integration is not configured yet. Pass an explicit OCI chart ref "
            "to upgrade workflows or implement this integration."
        )
