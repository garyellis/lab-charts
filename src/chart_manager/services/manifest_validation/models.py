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

from chart_manager.api.lifecycle.v1alpha1 import ManifestValidationSpec
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.services.domain.charts import HelmChart

PhaseName = Literal["render", "schema", "policy"]
PhaseStatus = Literal["PASS", "FAIL", "SKIP", "NOT_RUN"]
ErrorType = Literal["tool", "spec"]
SkipCause = Literal["validator_disabled", "upstream_failed"]

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
    # Distinguishes a validation FAIL from a tool runtime crash or a spec
    # parse error. Phase functions set this alongside status;
    # RunResult.outcome() folds it into the outcome the surface exits on.
    error_type: ErrorType | None = None
    # Machine-readable reason for a skipped phase. Human-readable ``detail``
    # remains presentation text and must not be used to make orchestration or
    # evidence decisions.
    skip_cause: SkipCause | None = None
    # Wall-clock seconds for the phase. Populated by the runner (not the
    # phase fn itself) and surfaced in output only when --timings is on.
    elapsed_seconds: float | None = None


@dataclass(frozen=True)
class RowResult:
    """All phase results for one worklist row, keyed by phase name."""

    row: WorklistRow
    phases: Mapping[str, PhaseResult]
    # Concrete-tool outcomes. ``phases`` remains the stable category aggregate
    # consumed by existing CLI and wire surfaces.
    validator_results: Mapping[str, PhaseResult] = field(default_factory=dict)


@dataclass(frozen=True)
class RunResult:
    """Aggregate result of a validate run across all rows."""

    rows: tuple[RowResult, ...]
    rendered_root: Path
    # Configuration-level errors (corrupt chart-lifecycle.yaml, wrong resource envelope)
    # that prevent rows from being constructed at all.
    spec_errors: tuple[str, ...] = field(default_factory=tuple)

    def outcome(self) -> Outcome:
        """Fold all results into the one semantic outcome of the run.

        An `Outcome`, not a number: what a bad spec or a crashed kubeconform
        is *worth* as a process exit status is surface policy and lives in
        `plumbing/exit_codes.py`. This layer only decides which of the four
        things happened, so a non-CLI caller gets the same judgement without
        inheriting a process convention it has no use for.

        Precedence, most fundamental fault first: a spec error beats a tool
        error beats a validation failure beats a pass. The reasoning is that
        the later phases ran on input the earlier fault already invalidated,
        so reporting the downstream symptom would send the operator to the
        wrong file.
        """
        if self.spec_errors:
            return Outcome.SPEC
        has_tool_error = False
        has_fail = False
        for row in self.rows:
            for phase in row.phases.values():
                if phase.error_type == "spec":
                    return Outcome.SPEC
                if phase.error_type == "tool":
                    has_tool_error = True
                if phase.status == "FAIL":
                    has_fail = True
        if has_tool_error:
            return Outcome.TOOL
        if has_fail:
            return Outcome.FAILED
        return Outcome.SUCCESS
