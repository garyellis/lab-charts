"""Exception hierarchy for expected chart-manager failures."""


class ChartManagerError(Exception):
    """Base exception for expected CLI failures."""


class SpecError(ChartManagerError):
    """Raised when authored chart-manager configuration is missing or invalid."""


class CapabilityUnavailableError(ChartManagerError):
    """Raised when a requested chart-manager capability is not enabled."""


class ChartNotFoundError(ChartManagerError):
    """Raised when a chart name cannot be resolved."""


class DependencyCycleError(SpecError):
    """Raised when cluster-test requirements contain a cycle."""


class ExternalCommandError(ChartManagerError):
    """Raised when an external command fails."""

    def __init__(
        self,
        message: str = "",
        *,
        stderr: str = "",
        returncode: int | None = None,
    ) -> None:
        """Attach optional stderr/returncode for callers that inspect them."""
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


class MissingToolError(ExternalCommandError):
    """Raised when an external tool is not on PATH.

    Distinct from a tool that ran and failed: the surface maps this to exit
    127 ("command not found"), so a missing binary is not reported as a
    missing data file, and best-effort service handlers can degrade on it
    the same way they degrade on any other ExternalCommandError.
    """


class CommandTimeout(ExternalCommandError):
    """Raised when an external command exceeded its timeout.

    A type rather than a substring: callers deciding control flow on
    "did this time out" must not have to match on message wording owned by
    another module.
    """
