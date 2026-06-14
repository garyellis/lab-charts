class LabChartsError(Exception):
    """Base exception for expected CLI failures."""


class SpecError(LabChartsError):
    """Raised when a chart test spec is missing or invalid."""


class ChartNotFoundError(LabChartsError):
    """Raised when a chart name cannot be resolved."""


class DependencyCycleError(SpecError):
    """Raised when test-spec requirements contain a cycle."""


class ExternalCommandError(LabChartsError):
    """Raised when an external command fails."""
