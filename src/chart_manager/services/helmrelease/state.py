"""The promotion status model shared by monitor, test, and promote.

Before this module the vertical carried three disconnected vocabularies: two
`Literal` verdict sets (`monitor.Verdict` and `test.TestVerdict`, overlapping
by four members with no shared supertype), a free-form `reason: str` whose
value set was implicit across ~24 literal call sites, and
`events.lifecycle.PromotionPhase`, which nothing in monitor or test could
reach. Every consumer -- renderer, wire projection, CLI exit code -- then
re-derived run state from those primitives independently, which is how "which
verdicts count as success" ended up copy-pasted into six places across two
layers and how the promotion timeline ended up with a start and no end.

`StrEnum` is deliberate, not cosmetic. Members compare equal to their wire
strings, hash like them (so `"ready" in PASSING_VERDICTS` still works for a
caller holding a plain string), and `json.dump` writes the value verbatim --
so adopting these types changes no byte of the `--output json` contract pinned
by `tests/fixtures/golden/helmrelease-*.json.golden`.

The three phase tables below are data, not code, on purpose: mapping a
terminal state to a lifecycle event is the kind of decision that gets
silently forked the moment it is expressed as an if-chain in each caller,
which is exactly what `promote.py` and `cli/helmrelease.py` had done.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from chart_manager.integrations.helmrelease import HelmReleaseRef
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.services.events.lifecycle import PromotionPhase

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

    One enum for both services. `READY` is what a converged rollout reports
    and `PASSED` is what a green `helm test` reports -- the deliberate rename
    that previously forced two disjoint types; everything else is shared.
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
        """True when this verdict counts toward a successful run.

        The single home for the rule that used to be six hardcoded tuples --
        three in `monitor.py`, one in `helm_test.py`, and two more in
        `cli/helmrelease_render.py` where `ok_count` re-implemented
        `MonitorResult.ok`'s predicate. A seventh verdict added to only some
        of them made the headline count and the process exit code disagree.
        """
        return self in PASSING_VERDICTS


#: A skip is not a failure: a suspended HelmRelease was deliberately taken out
#: of the rollout, so it must not fail the run. `SKIPPED_NOT_READY` is absent
#: on purpose -- it means the release never reached the state under test.
PASSING_VERDICTS: frozenset[Verdict] = frozenset(
    {Verdict.READY, Verdict.PASSED, Verdict.SKIPPED_SUSPENDED}
)

#: Synthetic ref that carries `Verdict.NO_MATCH`. A run that matched nothing
#: reports one outcome rather than an empty tuple, so every surface renders
#: "nothing matched" through the same path it renders a failure -- an empty
#: result set is the shape most callers forget to handle.
#:
#: Lives with the verdict it accompanies: the two are one decision.
NO_MATCH_REF = HelmReleaseRef(
    name="<no-match>",
    namespace="",
    api_version="",
    release_name="",
    storage_namespace="",
    target_namespace="",
)

#: Worst-first fold order for `run_verdict`. FAILED outranks TIMED_OUT because
#: it is the more specific diagnosis: a timeout says only that we stopped
#: looking, a failure says the cluster told us why.
_SEVERITY: tuple[Verdict, ...] = (
    Verdict.FAILED,
    Verdict.TIMED_OUT,
    Verdict.NO_MATCH,
    Verdict.SKIPPED_NOT_READY,
)


class Reason(StrEnum):
    """The reason values this codebase authors itself.

    Deliberately NOT the closed set of everything a `reason` field can hold:
    `MonitorService` passes Flux's `Ready` condition reason straight through
    from the CRD, so the field is typed `ReasonLike` and unknown values stay
    raw strings. Modelling that explicitly is the point -- pretending the set
    is closed would invite an exhaustive `match` that silently mis-handles
    whatever Flux ships next.
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
#: Declared here rather than in monitor.py because it is part of the reason
#: vocabulary, not of the polling loop.
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
    """Return the `Reason` member for `value`, or `value` itself if unmodelled.

    The open tail of `ReasonLike` made real: a Flux reason we have never seen
    survives as a plain string rather than raising, and one we do model
    arrives at consumers as a comparable member.
    """
    try:
        return Reason(value)
    except ValueError:
        return value


class Stage(StrEnum):
    """Which half of the promotion lifecycle produced a verdict.

    `Verdict.FAILED` means "the rollout never converged" from `MonitorService`
    and "helm test exited non-zero" from `TestService` -- two different
    `PromotionPhase` values. Keying the phase tables on (stage, verdict)
    keeps that ambiguity visible instead of resolving it by accident, and it
    is why one `PromotionTelemetry` can serve both services.
    """

    ROLLOUT = "rollout"
    HELM_TEST = "helm-test"


#: The phase emitted when a stage starts working, i.e. the opening bracket of
#: the interval DESIGN.md wants measured.
START_PHASE: Mapping[Stage, PromotionPhase] = {
    Stage.ROLLOUT: PromotionPhase.WAITING_ROLLOUT,
    Stage.HELM_TEST: PromotionPhase.HELM_TEST_RUN,
}

#: (stage, run verdict) -> every phase that finished run should emit, in
#: lifecycle order.
#:
#: A rollout that fails or times out maps to ABANDONED: `PromotionPhase` has
#: no ROLLOUT_FAILED member, and ABANDONED is already its "this version did
#: not reach the environment" terminal. The emitted `detail` carries the
#: stage, verdict and failure count, so a consumer can still tell a declined
#: downgrade (promote.py's ABANDONED) from a stuck rollout.
#:
#: A green `helm test` is what makes a promotion *verified* live, so it also
#: carries PROMOTED. A monitor-only pipeline therefore closes at ROLLOUT_OK
#: and never reports PROMOTED -- deliberate: nothing has checked that the
#: workload actually works.
#:
#: (stage, verdict) pairs absent from this table emit nothing on purpose. A
#: run where every HelmRelease was suspended, or where none matched, is not a
#: state transition -- recording one would put a phantom endpoint on the
#: timeline.
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

    A run is only as good as its worst release, so any non-passing verdict
    wins over `success`. A run that mixes skips with real successes reports
    `success`: reporting SKIPPED_SUSPENDED there would make a healthy
    promotion look stalled on the timeline.

    A run where *every* release was suspended is the exception, and reports
    SKIPPED_SUSPENDED. Folding it to `success` claimed a green rollout and a
    verified-live PROMOTED from zero executed tests -- see `TERMINAL_PHASES`,
    which has no SKIPPED_SUSPENDED row precisely so this emits nothing.
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
    """A timestamped phase change observed while watching or testing a HelmRelease.

    Domain, not plumbing: `phase` is the vocabulary an operator reads in the
    live progress table and in the "Recent transitions" section of a failure
    report, and `MonitorOutcome`/`TestOutcome` both carry a tuple of these
    across the wire contract. It sits beside `Verdict`/`Reason` because it is
    the same model observed mid-run instead of at the end.
    """

    at: datetime
    phase: str
    detail: str


#: Cap for a `Transition.detail` and for any other operator-facing one-liner
#: built from a condition message or kubectl stderr. Both are unbounded; a
#: progress-table row and a report bullet are one line wide.
DETAIL_MAX = 200


class PromoteStatus(StrEnum):
    """The single terminal state of one `promote` run.

    Replaces five independent booleans (`no_changes`, `dry_run`,
    `already_open`, `aborted`, plus `pull_request is not None`) that encoded
    2**5 combinations for six real states and were decoded in two different
    orders -- `promote.py`'s event mapping and the CLI's printer -- so adding
    a seventh state meant editing both and the type permitted pairs neither
    of them handled.
    """

    NO_CHANGES = "no-changes"
    DRY_RUN = "dry-run"
    ABORTED = "aborted"
    ALREADY_OPEN = "already-open"
    PR_OPENED = "pr-opened"
    PUSHED = "pushed"


#: promote status -> the phase it records, or None for states that are not a
#: transition at all. A dry run changed nothing and a no-op promotion moved
#: nothing, so neither may leave a mark on the timeline.
#:
#: PUSHED shares FLUX_PR_OPEN with PR_OPENED: it is the same transition
#: observed through a `gh` response that carried no URL, not a different one.
PROMOTE_PHASE: Mapping[PromoteStatus, PromotionPhase | None] = {
    PromoteStatus.NO_CHANGES: None,
    PromoteStatus.DRY_RUN: None,
    PromoteStatus.ABORTED: PromotionPhase.ABANDONED,
    PromoteStatus.ALREADY_OPEN: PromotionPhase.AWAITING_MERGE,
    PromoteStatus.PR_OPENED: PromotionPhase.FLUX_PR_OPEN,
    PromoteStatus.PUSHED: PromotionPhase.FLUX_PR_OPEN,
}


#: promote status -> did the caller get what they asked for. The third table
#: classifying the same six states, and the same kind of thing as
#: `PROMOTE_PHASE` above: data, not an if-chain re-derived per consumer.
#:
#: This is the *only* place that answers "was this promote a success", and
#: both consumers read it: `wire.promote_to_dict` publishes
#: `ok = outcome is Outcome.SUCCESS`, and `cli/helmrelease.py` exits with
#: `exit_code_for(outcome)`. Splitting that judgement in two is how promote
#: shipped a state (`ABORTED`) that printed a failure and exited 0.
#:
#: Note there are no exit codes here. `Outcome` is a semantic vocabulary from
#: `plumbing/exit_codes.py`; what number `FAILED` is worth is that module's
#: business, not this vertical's -- see its docstring for why the table is
#: keyed on `Outcome` rather than on `PromoteStatus` directly.
#:
#: Why each arm:
#:   PR_OPENED / PUSHED  -- the PR exists; that is the whole request.
#:   ALREADY_OPEN        -- idempotent re-run; the requested PR is open, and
#:                          `PROMOTE_PHASE` records it as a real forward
#:                          transition (AWAITING_MERGE), not a failure.
#:   NO_CHANGES          -- every match is already at the target version, so
#:                          the desired state holds. A promote must be safe
#:                          to re-run in CI.
#:   DRY_RUN             -- design §6.3: a dry run prints the plan and exits 0.
#:   ABORTED             -- design §6.1 names this verbatim: "promote
#:                          aborted/declined" is a FAILED case. Nothing was
#:                          promoted.
#:
#: Deliberately no `Outcome.USAGE` arm: a usage error is raised by the
#: surface during argument handling and never reaches a `PromoteResult`.
PROMOTE_OUTCOME: Mapping[PromoteStatus, Outcome] = {
    PromoteStatus.NO_CHANGES: Outcome.SUCCESS,
    PromoteStatus.DRY_RUN: Outcome.SUCCESS,
    PromoteStatus.ABORTED: Outcome.FAILED,
    PromoteStatus.ALREADY_OPEN: Outcome.SUCCESS,
    PromoteStatus.PR_OPENED: Outcome.SUCCESS,
    PromoteStatus.PUSHED: Outcome.SUCCESS,
}
