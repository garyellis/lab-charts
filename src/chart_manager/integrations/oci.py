from __future__ import annotations

from chart_manager.plumbing.errors import ChartManagerError


class OciRegistry:
    def chart_ref(self, chart: str, version: str | None = None) -> str:
        raise ChartManagerError(
            "OCI registry integration is not configured yet. Pass an explicit OCI chart ref "
            "to upgrade workflows or implement this integration."
        )
