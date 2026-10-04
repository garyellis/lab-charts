"""What `validate.run()` takes and returns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from chart_manager.plumbing.exit_codes import Outcome

CheckName = Literal["render", "schema", "policy"]
#: `failed`: the check found problems in the chart. `error`: the tool itself broke.
Status = Literal["passed", "failed", "skipped", "error"]


@dataclass(frozen=True)
class ValidateRequest:
    """What to validate: the named charts, else the rows `changes` select (None: every row)."""

    charts: tuple[str, ...]
    envs: tuple[str, ...] = ()
    checks: frozenset[CheckName] = frozenset(get_args(CheckName))
    changes: tuple[str, ...] | None = None
    out: Path | None = None
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
class ValidateOutcome:
    """Every row of one validate run, and the charts whose configuration kept them out."""

    rows: tuple[Row, ...]
    spec_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def outcome(self) -> Outcome:
        """The run's exit reason: a spec error, then a tool error, then a failed check."""
        statuses = {result.status for row in self.rows for result in row.checks.values()}
        if self.spec_errors:
            return Outcome.SPEC
        if "error" in statuses:
            return Outcome.TOOL
        if "failed" in statuses:
            return Outcome.FAILED
        return Outcome.SUCCESS
