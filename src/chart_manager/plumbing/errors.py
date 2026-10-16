"""Exception hierarchy for expected chart-manager failures."""

from chart_manager.plumbing.exit_codes import Outcome


class ChartManagerError(Exception):
    """Base exception for expected CLI failures.

    `outcome` is the exit outcome `main` reports. A subclass sets it only
    when it differs from its parent's.
    """

    outcome = Outcome.FAILED


class YamlError(ChartManagerError):
    """Raised when YAML cannot be decoded, parsed, encoded, read, or written."""


class SpecError(ChartManagerError):
    """Raised when authored chart-manager configuration is missing or invalid."""

    outcome = Outcome.SPEC


class WorkspaceNotFoundError(ChartManagerError):
    """Raised when no `.chart-manager/workspace.yaml` marks the repository."""

    outcome = Outcome.ENVIRONMENT


class CapabilityUnavailableError(ChartManagerError):
    """Raised when a requested chart-manager capability is not enabled."""


class ChartNotFoundError(ChartManagerError):
    """Raised when a chart name cannot be resolved."""


class DependencyCycleError(SpecError):
    """Raised when chart-test requirements contain a cycle."""


class ExternalCommandError(ChartManagerError):
    """Raised when an external command fails."""

    outcome = Outcome.TOOL

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

    Distinct from a tool that ran and failed: it exits 127 ("command not
    found"), so a missing binary is not reported as a missing data file, and
    best-effort handlers can degrade on it the same way they degrade on any
    other ExternalCommandError.
    """

    outcome = Outcome.MISSING_BINARY


class CommandTimeout(ExternalCommandError):
    """Raised when an external command exceeded its timeout.

    A type rather than a substring: callers deciding control flow on
    "did this time out" must not have to match on message wording owned by
    another module.
    """
