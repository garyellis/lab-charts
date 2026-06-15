"""Frozen dataclasses for the validate pipeline.

These cross integration/service/CLI seams, so we use stdlib dataclasses
rather than pydantic. Pydantic models live at IO boundaries (spec parsing,
JSON output). Internal state transfer stays plain.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

PhaseName = Literal["render", "schema", "policy"]
PhaseStatus = Literal["PASS", "FAIL", "SKIP", "NOT_RUN"]
ErrorType = Literal["tool", "spec"]


@dataclass(frozen=True)
class WorklistRow:
    chart: str
    env: str
    release: str
    namespace: str


@dataclass(frozen=True)
class PhaseResult:
    phase: PhaseName
    status: PhaseStatus
    detail: str | None = None
    artifacts: tuple[Path, ...] = ()
    # Distinguishes a validation FAIL (exit 1) from a tool runtime crash
    # (exit 2) or a spec parse error (exit 3). Phase functions set this
    # alongside status; RunResult.exit_code() reads it.
    error_type: ErrorType | None = None
    # Wall-clock seconds for the phase. Populated by the runner (not the
    # phase fn itself) and surfaced in output only when --timings is on.
    elapsed_seconds: float | None = None


@dataclass(frozen=True)
class RowResult:
    row: WorklistRow
    phases: Mapping[str, PhaseResult]


@dataclass(frozen=True)
class RunResult:
    rows: tuple[RowResult, ...]
    rendered_root: Path
    # Spec-level errors (corrupt validate-spec.yaml, unknown version envelope)
    # that prevent rows from being constructed at all.
    spec_errors: tuple[str, ...] = field(default_factory=tuple)

    def exit_code(self) -> int:
        # Precedence: spec error (3) > tool error (2) > validation failure (1) > pass (0).
        if self.spec_errors:
            return 3
        has_tool_error = False
        has_fail = False
        for row in self.rows:
            for phase in row.phases.values():
                if phase.error_type == "spec":
                    return 3
                if phase.error_type == "tool":
                    has_tool_error = True
                if phase.status == "FAIL":
                    has_fail = True
        if has_tool_error:
            return 2
        if has_fail:
            return 1
        return 0
