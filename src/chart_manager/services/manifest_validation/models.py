"""Frozen dataclasses for the manifest-validation pipeline.

These cross integration/service/CLI seams, so we use stdlib dataclasses
rather than pydantic. Pydantic models live at IO boundaries (spec parsing,
JSON output). Internal state transfer stays plain.

This is also the caller-facing vocabulary of the capability: what a surface
hands in (`RunRequest`), what it gets back (`RunOutcome`), and the one error
that says "your input was bad, and here is which input"
(`ValidateInputError`). They were a separate `requests.py` on the theory that
a REST handler wants the contract without the orchestrator -- but that module
already imported this one, so the split bought a second import line and
nothing else.

Folds that more than one projection needs (`RunResult.tally`,
`row_elapsed_text`, `no_work_reason`) live here for the same reason: JSON,
markdown and the terminal table each used to compute them independently, and
the wire module's docstring promises surfaces "cannot diverge".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

from chart_manager.api.lifecycle.v1alpha1 import ManifestValidationSpec
from chart_manager.domain.charts import HelmChart
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.exit_codes import Outcome

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
class RowTally:
    """How many rows passed, failed and were skipped in one run."""

    rows: int
    passing: int
    failing: int
    skipped: int


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

    def tally(self) -> RowTally:
        """Count rows by verdict: any FAIL fails the row, all-quiet skips it.

        One fold, because the JSON payload's `summary` and the markdown
        tally line are the same claim about the same run and used to be
        written out twice.
        """
        passing = 0
        failing = 0
        skipped = 0
        for row in self.rows:
            statuses = {phase.status for phase in row.phases.values()}
            if "FAIL" in statuses:
                failing += 1
            elif statuses and statuses <= {"SKIP", "NOT_RUN"}:
                skipped += 1
            elif "PASS" in statuses:
                passing += 1
        return RowTally(
            rows=len(self.rows),
            passing=passing,
            failing=failing,
            skipped=skipped,
        )


def row_elapsed_text(row_result: RowResult) -> str:
    """Sum the row's phase timings; empty string when nothing was timed.

    Shared by the markdown table and the terminal table so the "Elapsed"
    column reads identically in both. It lives here rather than beside
    either renderer because it belongs to neither: the wire module must not
    export a helper the terminal needs, and the terminal must not be the
    home of something markdown imports.
    """
    total = 0.0
    any_timed = False
    for phase in row_result.phases.values():
        if phase.elapsed_seconds is not None:
            total += phase.elapsed_seconds
            any_timed = True
    return f"{total:.1f}s" if any_timed else ""


class ValidateInputError(ChartManagerError):
    """A caller-supplied validate input could not be resolved.

    `hint` names the offending input so a surface can point at the right
    flag (or JSON field) without string-matching the message.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        """Store the message plus the name of the input that was rejected."""
        super().__init__(message)
        self.hint = hint


def _check_phases(phases: frozenset[str]) -> None:
    """Reject an empty or unknown phase set."""
    if not phases:
        raise ValidateInputError("at least one phase must be enabled", hint="phases")
    unknown = phases - ALL_PHASES
    if unknown:
        raise ValidateInputError(
            f"unknown phase(s): {', '.join(sorted(unknown))}; "
            f"valid: {', '.join(sorted(ALL_PHASES))}",
            hint="phases",
        )


@dataclass(frozen=True)
class RunRequest:
    """Spec-driven multi-row run (`validate run`).

    `charts`/`envs` narrow the built worklist. `changed_files` (a file of
    newline-delimited paths) and `base` (a git ref) feed the changed-files
    resolution; `skip_change_detection` short-circuits both. Timeouts use the
    pipeline's 0-means-unbounded convention. ``fail_fast`` stops before
    preparing later independent rows after the first failure.

    Validates itself in `__post_init__`, so an ill-formed request cannot
    reach `ManifestValidationService` -- the rule has one owner, and the
    surface's job is only to map `hint` onto whatever it calls that input
    (a flag, a JSON field).
    """

    root: Path = Path(".")
    charts: tuple[str, ...] = ()
    envs: tuple[str, ...] = ()
    base: str = "origin/main"
    changed_files: Path | None = None
    skip_change_detection: bool = False
    phases: frozenset[str] = ALL_PHASES
    out: Path | None = None
    keep: bool = False
    workers: int = 0
    verbose: bool = False
    row_timeout: float = 0.0
    dep_update_timeout: float = 300.0
    fail_fast: bool = False

    def __post_init__(self) -> None:
        """Reject an unknown phase name."""
        _check_phases(self.phases)


@dataclass(frozen=True)
class RunOutcome:
    """Everything one validate run produced.

    `result` is the wire-projectable payload (`services/manifest_validation/wire.py`);
    the remaining fields are run metadata a surface may want to narrate --
    non-fatal build warnings, how many charts carried no spec, how many
    rows the chart/env filters dropped, how many workers the run actually
    got -- plus the artifact lifetime inputs `cleanup()` needs.
    """

    result: RunResult
    out_dir: Path
    keep: bool = False
    warnings: tuple[str, ...] = ()
    ignored_changes: tuple[Path, ...] = ()
    unmatched_changes: tuple[Path, ...] = ()
    unmatched_charts: tuple[str, ...] = ()
    unmatched_environments: tuple[str, ...] = ()
    charts_unvalidated: int = 0
    rows_filtered_out: int = 0
    enabled_phases: frozenset[str] = ALL_PHASES
    # The worker count the run *used*, not the one it was asked for: verbose
    # runs are clamped to 1. A surface narrating "your --workers was ignored"
    # reads this instead of re-deriving the clamp.
    workers: int = 1

    @property
    def outcome(self) -> Outcome:
        """The semantic outcome folded from the underlying RunResult.

        The surface turns this into a number with `exit_code_for`; nothing
        in this layer needs to know which number.
        """
        return self.result.outcome()

    @property
    def ok(self) -> bool:
        """True when nothing failed."""
        return self.outcome is Outcome.SUCCESS


def no_work_reason(
    outcome: RunOutcome,
    *,
    requested_charts: Sequence[str] = (),
    requested_environments: Sequence[str] = (),
) -> str | None:
    """Explain an empty run, most specific cause first; None when rows ran.

    Shared by the JSON diagnostics object and the markdown "nothing to
    validate" line so an operator and a script are told the same story.
    """
    if outcome.result.rows:
        return None
    if outcome.unmatched_charts or outcome.unmatched_environments:
        return "requested filters did not match"
    if requested_charts or requested_environments:
        return "requested filters selected no affected validation cases"
    if outcome.unmatched_changes:
        return "changed files matched no validation trigger"
    if outcome.ignored_changes:
        return "all relevant changed files were explicitly ignored"
    if outcome.charts_unvalidated:
        return "no chart with manifest-validation configuration was selected"
    return "no affected validation cases"
