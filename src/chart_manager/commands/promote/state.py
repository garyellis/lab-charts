"""The promotion status model shared by monitor, test, and promote.

The enums are `StrEnum`s so members compare and hash equal to their wire
strings and `json.dump` writes them verbatim. The phase tables are data so
callers don't each re-derive them.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from chart_manager.integrations.kubectl import HelmReleaseRef
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.shared.events.model import PromotionPhase

__all__ = [
    "DETAIL_MAX",
    "NO_MATCH_REF",
    "PASSING_VERDICTS",
    "PROMOTE_OUTCOME",
    "PROMOTE_PHASE",
    "START_PHASE",
    "TERMINAL_PHASES",
    "TERMINAL_READY_REASONS",
    "PromoteStatus",
    "Reason",
    "ReasonLike",
    "Stage",
    "Transition",
    "Verdict",
    "coerce_reason",
    "run_verdict",
]


class Verdict(StrEnum):
    """Terminal state of one watched or tested HelmRelease.

    `READY` is a converged rollout; `PASSED` is a green `helm test`.
    """

    READY = "ready"
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed-out"
    SKIPPED_SUSPENDED = "skipped-suspended"
    SKIPPED_NOT_READY = "skipped-not-ready"
    NO_MATCH = "no-match"

    @property
    def is_passing(self) -> bool:
        """True when this verdict counts toward a successful run."""
        return self in PASSING_VERDICTS


#: A skip is not a failure: a suspended HelmRelease was deliberately taken out
#: of the rollout, so it must not fail the run. `SKIPPED_NOT_READY` is absent
#: on purpose -- it means the release never reached the state under test.
PASSING_VERDICTS: frozenset[Verdict] = frozenset(
    {Verdict.READY, Verdict.PASSED, Verdict.SKIPPED_SUSPENDED}
)

#: Synthetic ref that carries `Verdict.NO_MATCH`, so a run that matched nothing
#: reports one outcome rather than an empty tuple.
NO_MATCH_REF = HelmReleaseRef(
    name="<no-match>",
    namespace="",
    api_version="",
    release_name="",
    storage_namespace="",
    target_namespace="",
)

#: Worst-first fold order for `run_verdict`. FAILED outranks TIMED_OUT as the
#: more specific diagnosis.
_SEVERITY: tuple[Verdict, ...] = (
    Verdict.FAILED,
    Verdict.TIMED_OUT,
    Verdict.NO_MATCH,
    Verdict.SKIPPED_NOT_READY,
)


class Reason(StrEnum):
    """The reason values this codebase authors itself.

    Not closed: Flux reasons pass through as raw strings (see `ReasonLike`).
    """

    # --- shared -----------------------------------------------------------
    NO_HELMRELEASES_MATCHED = "NoHelmReleasesMatched"
    SUSPENDED = "Suspended"
    TOTAL_BUDGET_EXHAUSTED = "TotalBudgetExhausted"
    PER_HR_BUDGET_EXHAUSTED = "PerHRBudgetExhausted"

    # --- monitor ----------------------------------------------------------
    READY = "Ready"
    DISAPPEARED = "Disappeared"
    STALLED = "Stalled"

    # --- test -------------------------------------------------------------
    NOT_RELEASED = "NotReleased"
    GENERATION_LAG = "GenerationLag"
    REAP_LIST_FAILED = "ReapListFailed"
    TEST_POD_IN_FLIGHT = "TestPodInFlight"
    REAP_INCOMPLETE = "ReapIncomplete"
    HELM_UNAVAILABLE = "HelmUnavailable"
    TEST_POD_CONFLICT = "TestPodConflict"
    ALL_TESTS_PASSED = "AllTestsPassed"
    NO_TESTS_DEFINED = "NoTestsDefined"
    TEST_FAILED = "TestFailed"

    # --- Flux-supplied, modelled because we branch on them ----------------
    INSTALL_FAILED = "InstallFailed"
    UPGRADE_FAILED = "UpgradeFailed"
    RECONCILIATION_FAILED = "ReconciliationFailed"
    ARTIFACT_FAILED = "ArtifactFailed"
    RETRY_EXHAUSTED = "RetryExhausted"


#: A reason is either one we authored or whatever Flux put in the CRD.
ReasonLike = Reason | str

#: `Ready=False` reasons Flux will not retry out of, so the watcher stops.
TERMINAL_READY_REASONS: frozenset[Reason] = frozenset(
    {
        Reason.INSTALL_FAILED,
        Reason.UPGRADE_FAILED,
        Reason.RECONCILIATION_FAILED,
        Reason.ARTIFACT_FAILED,
        Reason.RETRY_EXHAUSTED,
    }
)


def coerce_reason(value: str) -> ReasonLike:
    """Return the `Reason` member for `value`, or `value` itself if unmodelled."""
    try:
        return Reason(value)
    except ValueError:
        return value


class Stage(StrEnum):
    """Which half of the promotion lifecycle produced a verdict.

    The phase tables key on (stage, verdict) because `Verdict.FAILED` maps to
    a different `PromotionPhase` in each stage.
    """

    ROLLOUT = "rollout"
    HELM_TEST = "helm-test"


#: The phase emitted when a stage starts working, i.e. the opening bracket of
#: the interval being measured.
START_PHASE: Mapping[Stage, PromotionPhase] = {
    Stage.ROLLOUT: PromotionPhase.WAITING_ROLLOUT,
    Stage.HELM_TEST: PromotionPhase.HELM_TEST_RUN,
}

#: (stage, run verdict) -> every phase that finished run should emit, in
#: lifecycle order. A failed or timed-out rollout is ABANDONED; the event's
#: `detail` names the stage, so it reads apart from a declined promote.
#: Only a green `helm test` emits PROMOTED. Absent pairs (all
#: suspended, none matched) emit nothing: they are not a state transition.
TERMINAL_PHASES: Mapping[tuple[Stage, Verdict], tuple[PromotionPhase, ...]] = {
    (Stage.ROLLOUT, Verdict.READY): (PromotionPhase.ROLLOUT_OK,),
    (Stage.ROLLOUT, Verdict.FAILED): (PromotionPhase.ABANDONED,),
    (Stage.ROLLOUT, Verdict.TIMED_OUT): (PromotionPhase.ABANDONED,),
    (Stage.HELM_TEST, Verdict.PASSED): (
        PromotionPhase.HELM_TEST_OK,
        PromotionPhase.PROMOTED,
    ),
    (Stage.HELM_TEST, Verdict.FAILED): (PromotionPhase.HELM_TEST_FAILED,),
    (Stage.HELM_TEST, Verdict.TIMED_OUT): (PromotionPhase.HELM_TEST_FAILED,),
}


def run_verdict(verdicts: Iterable[Verdict], *, success: Verdict) -> Verdict:
    """Fold per-HelmRelease verdicts into the one verdict describing the run.

    Any non-passing verdict wins over `success`; skips mixed with successes
    report `success`. A run where every release was suspended reports
    SKIPPED_SUSPENDED, which `TERMINAL_PHASES` maps to no phase.
    """
    seen = set(verdicts)
    for verdict in _SEVERITY:
        if verdict in seen:
            return verdict
    if seen and seen <= {Verdict.SKIPPED_SUSPENDED}:
        return Verdict.SKIPPED_SUSPENDED
    return success


@dataclass(frozen=True)
class Transition:
    """A timestamped phase change observed while watching or testing a HelmRelease."""

    at: datetime
    phase: str
    detail: str


#: Cap for a `Transition.detail` and other one-liners built from a condition
#: message or kubectl stderr.
DETAIL_MAX = 200


class PromoteStatus(StrEnum):
    """The single terminal state of one `promote` run."""

    NO_CHANGES = "no-changes"
    DRY_RUN = "dry-run"
    ABORTED = "aborted"
    ALREADY_OPEN = "already-open"
    PR_OPENED = "pr-opened"
    PUSHED = "pushed"


#: promote status -> the phase it records, or None for states that are not a
#: transition. PUSHED is PR_OPENED seen through a `gh` response with no URL.
PROMOTE_PHASE: Mapping[PromoteStatus, PromotionPhase | None] = {
    PromoteStatus.NO_CHANGES: None,
    PromoteStatus.DRY_RUN: None,
    PromoteStatus.ABORTED: PromotionPhase.ABANDONED,
    PromoteStatus.ALREADY_OPEN: PromotionPhase.AWAITING_MERGE,
    PromoteStatus.PR_OPENED: PromotionPhase.FLUX_PR_OPEN,
    PromoteStatus.PUSHED: PromotionPhase.FLUX_PR_OPEN,
}


#: promote status -> did the caller get what they asked for. Both the wire `ok`
#: field (`wire.promote_to_dict`) and the exit code (`commands/promote/cli.py`)
#: derive from this table. Only ABORTED fails: re-runs that change nothing
#: (NO_CHANGES, ALREADY_OPEN) must be safe in CI.
PROMOTE_OUTCOME: Mapping[PromoteStatus, Outcome] = {
    PromoteStatus.NO_CHANGES: Outcome.SUCCESS,
    PromoteStatus.DRY_RUN: Outcome.SUCCESS,
    PromoteStatus.ABORTED: Outcome.FAILED,
    PromoteStatus.ALREADY_OPEN: Outcome.SUCCESS,
    PromoteStatus.PR_OPENED: Outcome.SUCCESS,
    PromoteStatus.PUSHED: Outcome.SUCCESS,
}
