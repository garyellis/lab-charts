"""What `plan.run()` takes and returns."""

from __future__ import annotations

from dataclasses import dataclass

from chart_manager.commands import test, validate


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
class PlanOutcome:
    """The changed files, and the validation rows, chart tests and charts to publish they select."""

    changed_files: tuple[str, ...]
    validation: validate.Selection
    chart_tests: test.Selection
    publish: tuple[str, ...]

    @property
    def spec_errors(self) -> tuple[str, ...]:
        """The chart configuration errors both selections found."""
        return (*self.validation.spec_errors, *self.chart_tests.spec_errors)
