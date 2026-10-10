"""What `plan.run()` takes and returns."""

from __future__ import annotations

from dataclasses import dataclass

from chart_manager.commands import test, validate
from chart_manager.plumbing.exit_codes import Outcome


@dataclass(frozen=True)
class PlanRequest:
    """The changed files to plan for; None reads them from `git diff <base>...HEAD`.

    Without changes, `all_charts` or `charts` pick the chart tests directly and nothing else
    is selected.
    """

    changes: tuple[str, ...] | None = None
    base: str = "origin/main"
    all_charts: bool = False
    charts: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedRow:
    """One chart in one environment to validate, and why changes selected it."""

    chart: str
    env: str
    release: str
    namespace: str
    reasons: tuple[validate.Reason, ...]


@dataclass(frozen=True)
class PlanOutcome:
    """The changed files, the work they select, and the chart errors and warnings found."""

    changed_files: tuple[str, ...]
    validation: tuple[PlannedRow, ...]
    chart_tests: tuple[test.SelectedTest, ...]
    publish: tuple[str, ...]
    spec_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def outcome(self) -> Outcome:
        """SPEC when either selection found chart configuration errors."""
        return Outcome.SPEC if self.spec_errors else Outcome.SUCCESS
