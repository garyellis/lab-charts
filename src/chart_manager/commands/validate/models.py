"""What `validate.run()` takes and returns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.exit_codes import Outcome

CheckName = Literal["render", "schema", "policy"]
#: `failed`: the check found problems in the chart. `error`: the tool itself broke.
Status = Literal["passed", "failed", "skipped", "error"]
#: The statuses that fail a row.
FAILING: frozenset[Status] = frozenset({"failed", "error"})


class RequestError(SpecError):
    """The request names a chart or environment the workspace does not have."""

    def __init__(self, message: str, *, flag: str) -> None:
        super().__init__(message)
        self.flag = flag


@dataclass(frozen=True)
class ValidateRequest:
    """What to validate: the named charts, else the rows `changes` select (None: every row).

    Each row renders into `out/<chart>/<env>`.
    """

    charts: tuple[str, ...]
    out: Path
    envs: tuple[str, ...] = ()
    checks: frozenset[CheckName] = frozenset(get_args(CheckName))
    changes: tuple[str, ...] | None = None
    workers: int = 0
    fail_fast: bool = False
    tool_timeout: float | None = None
    verbose: bool = False


@dataclass(frozen=True)
class CheckResult:
    """One validation check's result for one row."""

    status: Status
    detail: str = ""
    elapsed_seconds: float | None = None


@dataclass(frozen=True)
class Row:
    """One chart in one environment, with the result of each validation check."""

    chart: str
    env: str
    release: str
    namespace: str
    checks: Mapping[CheckName, CheckResult]


@dataclass(frozen=True)
class Diagnostics:
    """What shaped the selection: the filters asked for, and the changes and charts left out."""

    requested_charts: tuple[str, ...] = ()
    requested_envs: tuple[str, ...] = ()
    ignored_changes: tuple[str, ...] = ()
    unmatched_changes: tuple[str, ...] = ()
    rows_filtered_out: int = 0
    charts_unvalidated: int = 0


@dataclass(frozen=True)
class ValidateOutcome:
    """Every row of one validate run, and the charts whose configuration kept them out."""

    rows: tuple[Row, ...]
    spec_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    diagnostics: Diagnostics = Diagnostics()

    def outcome(self) -> Outcome:
        """The run's exit reason: a spec error, then a tool error, then a failed check."""
        statuses = {result.status for row in self.rows for result in row.checks.values()}
        if self.spec_errors:
            return Outcome.SPEC
        if "error" in statuses:
            return Outcome.TOOL
        if statuses & FAILING:
            return Outcome.FAILED
        return Outcome.SUCCESS
