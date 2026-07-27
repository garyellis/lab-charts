"""Expected failures raised by the chart upgrade service."""

from chart_manager.plumbing.errors import ChartManagerError


class UpgradeError(ChartManagerError):
    """An upgrade request is unsafe or inconsistent with repository state."""

