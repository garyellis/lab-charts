"""What `validate.run()` takes and returns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CheckName = Literal["render"]
Status = Literal["passed", "failed"]


@dataclass(frozen=True)
class ValidateRequest:
    """Which charts and environments to validate."""

    charts: tuple[str, ...]
    envs: tuple[str, ...] = ()
    out: Path | None = None


@dataclass(frozen=True)
class CheckResult:
    """One validation check's result for one row."""

    status: Status
    detail: str = ""


@dataclass(frozen=True)
class Row:
    """One chart in one environment, with the result of each validation check."""

    chart: str
    env: str
    checks: Mapping[CheckName, CheckResult]


@dataclass(frozen=True)
class ValidateOutcome:
    """Every row of one validate run."""

    rows: tuple[Row, ...]
