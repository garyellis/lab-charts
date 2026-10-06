"""The doctor report and the precedence that turns its checks into one outcome."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import Check, CheckStatus

#: Most fundamental failure first, the order an operator would fix them in:
#: a missing tool makes later checks unanswerable, then authored config, then
#: the environment, then a broken tool. The exit code is the first one reported.
_OUTCOME_PRECEDENCE: Final[tuple[Outcome, ...]] = (
    Outcome.MISSING_BINARY,
    Outcome.SPEC,
    Outcome.ENVIRONMENT,
    Outcome.TOOL,
    Outcome.FAILED,
)


@dataclass(frozen=True)
class DoctorReport:
    """Every check that ran, and what the run as a whole means."""

    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        """True when nothing failed. A skipped check is not a failure."""
        return all(check.status is not CheckStatus.FAILED for check in self.checks)

    @property
    def outcome(self) -> Outcome:
        """The single outcome this run exits with, by `_OUTCOME_PRECEDENCE`."""
        reported = {check.outcome for check in self.checks}
        for candidate in _OUTCOME_PRECEDENCE:
            if candidate in reported:
                return candidate
        return Outcome.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        """The `-o json` document: `ok`, `outcome`, `for` and `checks`.

        `for` is always null now that doctor runs every check; it stays so the
        document's shape does not change.
        """
        return {
            "ok": self.ok,
            "outcome": str(self.outcome),
            "for": None,
            "checks": [check.to_dict() for check in self.checks],
        }
