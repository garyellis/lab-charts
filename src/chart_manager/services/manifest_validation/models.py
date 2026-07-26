"""Frozen dataclasses for the manifest-validation pipeline.

These cross integration/service/CLI seams, so we use stdlib dataclasses
rather than pydantic. Pydantic models live at IO boundaries (spec parsing,
JSON output). Internal state transfer stays plain.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

from chart_manager.services.domain.charts import HelmChart
from chart_manager.services.manifest_validation.spec import ManifestValidationSpec

PhaseName = Literal["render", "schema", "policy"]
PhaseStatus = Literal["PASS", "FAIL", "SKIP", "NOT_RUN"]
ErrorType = Literal["tool", "spec"]

#: The phases, in dependency order (render feeds schema feeds policy).
#: Derived from `PhaseName` rather than restated so the two cannot drift.
#: This list used to be written out longhand in five places — the request
#: models, the runner's default, the wire projector's column order and the
#: CLI's error text — and `runner.py` re-hardcoded it specifically because
#: it cannot import from `app.py` (app imports runner). Owning it here, in
#: the validation domain, which makes "add a fourth phase" a tractable edit.
PHASE_ORDER: tuple[PhaseName, ...] = get_args(PhaseName)
ALL_PHASES: frozenset[str] = frozenset(PHASE_ORDER)


@dataclass(frozen=True)
class WorklistRow:
    """One chart+env unit of validation work."""

    chart: str
    env: str
    release: str
    namespace: str


@dataclass(frozen=True)
class ManifestValidationTarget:
    """A Helm chart composed with its authored manifest-validation spec."""

    chart: HelmChart
    spec: ManifestValidationSpec
    spec_path: Path

    @property
    def name(self) -> str:
        """Return the authoritative Helm chart name."""
        return self.chart.name

    @property
    def path(self) -> Path:
        """Return the authoritative Helm chart directory."""
        return self.chart.path


@dataclass(frozen=True)
class SelectionResult:
    """Rows selected for execution plus explicit selection diagnostics."""

    rows: tuple[WorklistRow, ...]
    unmatched_charts: tuple[str, ...] = ()
    unmatched_environments: tuple[str, ...] = ()
    ignored_changes: tuple[Path, ...] = ()
    unmatched_changes: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()
    filtered_out: int = 0


@dataclass(frozen=True)
class PhaseResult:
    """Outcome of a single phase (render/schema/policy) for one row."""

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
    """All phase results for one worklist row, keyed by phase name."""

    row: WorklistRow
    phases: Mapping[str, PhaseResult]


@dataclass(frozen=True)
class RunResult:
    """Aggregate result of a validate run across all rows."""

    rows: tuple[RowResult, ...]
    rendered_root: Path
    # Configuration-level errors (corrupt chart-manager.yaml, unknown version envelope)
    # that prevent rows from being constructed at all.
    spec_errors: tuple[str, ...] = field(default_factory=tuple)

    def exit_code(self) -> int:
        """Fold all results into the process exit code."""
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
