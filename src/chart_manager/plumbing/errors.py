class ChartManagerError(Exception):
    """Base exception for expected CLI failures."""


class SpecError(ChartManagerError):
    """Raised when a chart test spec is missing or invalid."""


class ChartNotFoundError(ChartManagerError):
    """Raised when a chart name cannot be resolved."""


class DependencyCycleError(SpecError):
    """Raised when test-spec requirements contain a cycle."""


class ExternalCommandError(ChartManagerError):
    """Raised when an external command fails."""
