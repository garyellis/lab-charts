"""Reading a HelmRelease status: is this release done, and if not, why not?

Pure by construction -- no HelmRelease client, no clock, no cancellation. Everything
here is a function of one status snapshot plus the workload rollouts observed
alongside it, which is what makes the Flux condition semantics reviewable in
one screen instead of spread across a polling loop that also owns backoff,
transport-error triage, budget enforcement and outcome assembly.

That mixing is what this module exists to undo: `MonitorService._watch_one`
was a 282-line function calling `_finalize` from eleven sites, each repeating
an identical six-keyword block, with nesting five levels deep. The branch
table below is the part a reviewer actually needs to check against Flux's
documented condition semantics, and it is now directly unit-testable.

Deliberately *not* in `state.py`: that module is the vocabulary shared by
monitor, test, promote, wire and the CLI renderer. These rules are the
rollout watcher's alone, and they drag in `integrations.helmrelease` status types
that the vocabulary does not otherwise need.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from chart_manager.integrations.helmrelease import HelmReleaseStatus, WorkloadRollout
from chart_manager.services.helmrelease.state import (
    DETAIL_MAX,
    TERMINAL_READY_REASONS,
    Reason,
    ReasonLike,
    Verdict,
    coerce_reason,
)

__all__ = [
    "Decision",
    "Terminal",
    "Waiting",
    "classify",
]


@dataclass(frozen=True)
class Terminal:
    """The release has reached a state the watcher will not poll out of.

    Carries the transition to record as well as the verdict, because every
    terminal state is also the last thing an operator sees in the progress
    table -- separating "what happened" from "what we showed" is how the two
    drifted apart in the first place.
    """

    verdict: Verdict
    reason: ReasonLike
    phase: str
    detail: str


@dataclass(frozen=True)
class Waiting:
    """The release is still converging; keep polling.

    `signature` is the dedupe key. Two consecutive polls with the same
    signature describe the same situation, so only the first is recorded --
    otherwise a five-minute rollout fills the ring buffer with one repeated
    phase and evicts the transitions that explain how it got there.
    """

    phase: str
    detail: str
    signature: tuple[object, ...]
    #: True when the HelmRelease itself reports converged on the requested
    #: version, so the answer now depends on workloads the caller must fetch.
    #: The caller re-classifies with the result; this decision is the fallback
    #: for when that fetch fails.
    needs_workloads: bool = field(default=False)


Decision = Terminal | Waiting


def classify(
    status: HelmReleaseStatus,
    *,
    requested_version: str,
    workloads: tuple[WorkloadRollout, ...] | None,
) -> Decision:
    """Decide whether `status` is terminal, and otherwise what it is waiting on.

    `workloads` is `None` when the owned workloads have not been read -- either
    because the HelmRelease has not yet reported converged (so any rollout
    state would be stale) or because listing them failed. A `Waiting` with
    `needs_workloads` set is the caller's cue to read them and ask again.
    """
    if status.suspended:
        return Terminal(
            verdict=Verdict.SKIPPED_SUSPENDED,
            reason=Reason.SUSPENDED,
            phase="Suspended",
            detail="HR spec.suspend=true",
        )

    stalled = status.condition("Stalled")
    if stalled is not None and stalled.status == "True":
        return Terminal(
            verdict=Verdict.FAILED,
            reason=Reason.STALLED,
            phase="Stalled",
            detail=stalled.message[:DETAIL_MAX],
        )

    ready_cond = status.ready
    if (
        ready_cond is not None
        and ready_cond.status == "False"
        and ready_cond.reason in TERMINAL_READY_REASONS
    ):
        return Terminal(
            verdict=Verdict.FAILED,
            reason=coerce_reason(ready_cond.reason),
            phase=f"Ready=False:{ready_cond.reason}",
            detail=ready_cond.message[:DETAIL_MAX],
        )

    # TestSuccess=False is only terminal once Released=True; before that it
    # just reflects the pre-run state of the test hook.
    test_cond = status.test_success
    released_cond = status.released
    if (
        test_cond is not None
        and test_cond.status == "False"
        and released_cond is not None
        and released_cond.status == "True"
    ):
        return Terminal(
            verdict=Verdict.FAILED,
            reason=(
                coerce_reason(test_cond.reason) if test_cond.reason else Reason.TEST_FAILED
            ),
            phase="TestSuccess=False",
            detail=test_cond.message[:DETAIL_MAX],
        )

    ready_status = ready_cond.status if ready_cond else "Unknown"
    ready_reason = ready_cond.reason if ready_cond else ""
    gen_caught_up = status.observed_generation == status.generation
    history_matches = status.history_chart_version == requested_version
    ready_true = ready_cond is not None and ready_cond.status == "True"

    # Only inspect workloads once the HR itself reports converged on the
    # requested version; earlier rollout state describes the previous release.
    converged_hr = gen_caught_up and history_matches and ready_true
    not_converged_names: tuple[str, ...] = ()
    if converged_hr and workloads is not None:
        not_converged_names = tuple(
            sorted(
                f"{w.workload.kind}/{w.workload.namespace}/{w.workload.name}"
                for w in workloads
                if not w.converged
            )
        )
        if not not_converged_names:
            return Terminal(
                verdict=Verdict.READY,
                reason=Reason.READY,
                phase="Ready",
                detail="HR Ready=True and all workloads converged",
            )

    return Waiting(
        phase=_phase_label(
            ready_status, ready_reason, gen_caught_up, history_matches, not_converged_names
        ),
        detail=_phase_detail(status, requested_version, not_converged_names),
        # `suspended` is deliberately absent: a suspended status returns
        # Terminal above, so it is constant False here and would only pad
        # the comparison.
        signature=(
            ready_status,
            ready_reason,
            status.observed_generation,
            frozenset(not_converged_names),
        ),
        needs_workloads=converged_hr and workloads is None,
    )


def _phase_label(
    ready_status: str,
    ready_reason: str,
    gen_caught_up: bool,
    history_matches: bool,
    not_converged_names: tuple[str, ...],
) -> str:
    """Classify the release's wait state into a short phase label (Ready when fully converged)."""
    if not gen_caught_up:
        return "GenerationLag"
    if not history_matches:
        return "HistoryLag"
    if ready_status != "True":
        return f"WaitingForReady:{ready_reason}" if ready_reason else "WaitingForReady"
    if not_converged_names:
        return f"WaitingForWorkloads:{len(not_converged_names)}"
    return "Ready"


def _phase_detail(
    status: HelmReleaseStatus,
    requested_version: str,
    not_converged_names: tuple[str, ...],
) -> str:
    """Render a compact one-line detail string (gen/history/ready/pending), capped in length."""
    ready = status.ready
    bits = [
        f"obs-gen={status.observed_generation}/{status.generation}",
        f"history={status.history_chart_version}",
        f"requested={requested_version}",
    ]
    if ready is not None:
        bits.append(f"ready={ready.status}({ready.reason})")
    if not_converged_names:
        bits.append(f"pending=[{','.join(not_converged_names)}]")
    detail = " ".join(bits)
    return detail[:DETAIL_MAX]
