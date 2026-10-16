"""`promote monitor`: watch matched HelmReleases until each converges, fails or times out.

Read-only. One watcher per matched Flux HelmRelease checks HR Ready/Released plus the
workload rollout under three budgets (per-poll, per-HR, total). Concurrency defaults to 4,
which suits laptop EKS/GKE exec-auth caches; raise it to 8 with care.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import chart_manager.commands.promote.report as report
from chart_manager.commands.promote.classify import Terminal, Waiting, classify
from chart_manager.commands.promote.fanout import (
    TRANSITIONS_MAX,
    RunResult,
    check_request,
    record,
    run_matched,
)
from chart_manager.commands.promote.matching import filter_matched_statuses
from chart_manager.commands.promote.state import (
    DETAIL_MAX,
    NO_MATCH_REF,
    Reason,
    ReasonLike,
    Stage,
    Transition,
    Verdict,
)
from chart_manager.commands.promote.telemetry import PromotionTelemetry
from chart_manager.integrations.kubectl import (
    HelmReleaseRef,
    HelmReleaseStatus,
    Kubectl,
    WorkloadRollout,
)
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.progress import Progress
from chart_manager.settings import Settings
from chart_manager.shared.events.writer import EventWriter

_LOG = logging.getLogger(__name__)

_DIAGNOSTICS_WORKLOAD_CAP = 5
#: Seconds between status polls of one HelmRelease; also the upper bound of
#: each watcher's initial jitter sleep.
_POLL_INTERVAL_SEC = 3.0
#: Smallest per-HR budget: a shorter one would expire before the first re-poll.
_MIN_PER_HR_SEC = _POLL_INTERVAL_SEC


@dataclass(frozen=True)
class MonitorRequest:
    """Parameters for one monitor run; validates timeout ordering on init."""

    chart_name: str
    version: str
    namespace: str | None = None
    concurrency: int = 4
    # Budgets in seconds. The CLI parses its duration strings once, at the
    # input boundary; every caller gets the same numeric validation below.
    per_poll_timeout_seconds: float = 10.0
    per_hr_timeout_seconds: float = 300.0
    total_timeout_seconds: float = 900.0
    # When True, the first failed/timed-out outcome triggers cancellation of
    # remaining in-flight watchers; their outcomes carry `TotalBudgetExhausted`.
    fail_fast: bool = False
    # Which promotion target this rollout belongs to. None (the default, and
    # what an ad-hoc `promote monitor` passes) means the run emits no
    # lifecycle events at all -- see commands/promote/telemetry.py.
    environment: str | None = None

    def __post_init__(self) -> None:
        """Reject empty identifiers and inconsistent budgets."""
        check_request(
            chart_name=self.chart_name,
            version=self.version,
            concurrency=self.concurrency,
            per_poll_timeout_seconds=self.per_poll_timeout_seconds,
            per_hr_timeout_seconds=self.per_hr_timeout_seconds,
            total_timeout_seconds=self.total_timeout_seconds,
            min_per_hr_seconds=_MIN_PER_HR_SEC,
        )


@dataclass(frozen=True)
class MonitorOutcome:
    """Terminal state of one watched HelmRelease."""

    ref: HelmReleaseRef
    verdict: Verdict
    # `ReasonLike`, not `Reason`: the terminal-Ready path hands back whatever
    # Flux wrote into the CRD condition, which we do not own and cannot close.
    reason: ReasonLike
    last_status: HelmReleaseStatus | None
    last_workloads: tuple[WorkloadRollout, ...]
    transitions: tuple[Transition, ...]
    diagnostics: str | None
    duration_seconds: float


#: Aggregate of all watcher outcomes for a monitor run. A plain assignment,
#: not a `type` statement, so `MonitorResult(...)` stays callable.
MonitorResult = RunResult[MonitorOutcome]


def _no_match_outcome(elapsed: float) -> MonitorOutcome:
    """The single synthetic outcome of a run whose selector matched nothing."""
    return MonitorOutcome(
        ref=NO_MATCH_REF,
        verdict=Verdict.NO_MATCH,
        reason=Reason.NO_HELMRELEASES_MATCHED,
        last_status=None,
        last_workloads=(),
        transitions=(),
        diagnostics=None,
        duration_seconds=elapsed,
    )


@dataclass
class _WatchState:
    """Mutable per-HelmRelease state threaded through one watcher's phases.

    Mirrors `test._RunContext`: the polling loop, the classifier plumbing and
    the finalizer share these values.
    """

    ref: HelmReleaseRef
    transitions: deque[Transition]
    last_status: HelmReleaseStatus | None
    last_workloads: tuple[WorkloadRollout, ...] = ()
    #: Dedupe key of the last recorded waiting/transport transition.
    prev_signature: object = None


def _fail_fast_predicate(request: MonitorRequest) -> Callable[[MonitorOutcome], bool]:
    """Build the `cancel_on` predicate for `run_fanout` from `request.fail_fast`.

    A skip or a no-match is not a reason to abandon the peers: fail-fast
    exists to stop burning the budget once a release has genuinely failed.
    """
    if not request.fail_fast:
        return lambda _outcome: False
    return lambda outcome: outcome.verdict in (Verdict.FAILED, Verdict.TIMED_OUT)


def run(
    request: MonitorRequest,
    *,
    runner: CommandRunner,
    settings: Settings,
    events: EventWriter,
    progress: Progress,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    rand: Callable[[float, float], float] = random.uniform,
) -> MonitorResult:
    """Watch every matching HelmRelease concurrently and aggregate the outcomes.

    Per-HR failures come back as outcomes; only infrastructure errors
    (kubectl/watcher crashes) raise, after cancelling peer watchers.
    `progress` hears each recorded transition; the time sources are for tests.
    """
    kubectl = Kubectl(runner, context=settings.kube_context, timeout=settings.command_timeout)
    watcher = _Watcher(kubectl, sleep, clock, rand, progress)
    start = clock()
    per_poll = request.per_poll_timeout_seconds
    matched = filter_matched_statuses(
        kubectl,
        namespace=request.namespace,
        chart_name=request.chart_name,
        version=request.version,
        per_poll=per_poll,
    )

    _LOG.info(
        "monitor run started: chart=%s version=%s namespace=%s matched=%d "
        "concurrency=%d fail_fast=%s per_poll=%gs per_hr=%gs total=%gs poll_interval=%.1fs",
        request.chart_name,
        request.version,
        request.namespace or "(all)",
        len(matched),
        request.concurrency,
        request.fail_fast,
        request.per_poll_timeout_seconds,
        request.per_hr_timeout_seconds,
        request.total_timeout_seconds,
        _POLL_INTERVAL_SEC,
    )

    # Built unconditionally, but inert until `run_matched` finds a match:
    # a run with nothing to watch opens no WAITING_ROLLOUT interval. Event
    # writes are non-fatal: the rollout has already happened.
    telemetry = PromotionTelemetry(
        writer=events,
        chart_name=request.chart_name,
        version=request.version,
        environment=request.environment,
    )
    total_deadline = start + request.total_timeout_seconds
    return run_matched(
        matched,
        start=start,
        clock=clock,
        total_deadline=total_deadline,
        concurrency=request.concurrency,
        telemetry=telemetry,
        stage=Stage.ROLLOUT,
        success=Verdict.READY,
        no_match=_no_match_outcome,
        work=lambda status, cancel_event: watcher.watch(
            status, request, per_poll, total_deadline, cancel_event
        ),
        crash_label="monitor watcher",
        cancel_on=_fail_fast_predicate(request),
        log_label="monitor",
        chart_name=request.chart_name,
        version=request.version,
        namespace=request.namespace,
    )


@dataclass(frozen=True)
class _Watcher:
    """Polls one HelmRelease to a verdict; one instance serves every watcher thread."""

    kubectl: Kubectl
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    rand: Callable[[float, float], float]
    progress: Progress

    def watch(
        self,
        initial_status: HelmReleaseStatus,
        request: MonitorRequest,
        per_poll: float,
        total_deadline: float,
        cancel_event: threading.Event,
    ) -> MonitorOutcome:
        """Poll one HR until ready/failed/suspended or a deadline expires."""
        started_mono = self.clock()
        state = _WatchState(
            ref=initial_status.ref,
            transitions=deque(maxlen=TRANSITIONS_MAX),
            last_status=initial_status,
        )
        verdict, reason = self._poll_until_terminal(
            initial_status,
            request,
            state,
            per_poll=per_poll,
            hr_deadline=min(started_mono + request.per_hr_timeout_seconds, total_deadline),
            total_deadline=total_deadline,
            cancel_event=cancel_event,
        )
        return self._finalize(
            state,
            verdict=verdict,
            reason=reason,
            per_poll=per_poll,
            started_mono=started_mono,
        )

    def _poll_until_terminal(
        self,
        initial_status: HelmReleaseStatus,
        request: MonitorRequest,
        state: _WatchState,
        *,
        per_poll: float,
        hr_deadline: float,
        total_deadline: float,
        cancel_event: threading.Event,
    ) -> tuple[Verdict, ReasonLike]:
        """Poll -> classify -> record -> budget-check, until something is terminal.

        The whole loop has exactly one job: decide *when* to stop. *Why* we
        stop is `classify`'s, and turning a stop into an outcome is
        `_finalize`'s. Keeping the three apart is what removed the eleven
        near-identical `_finalize` call sites this function used to carry.
        """
        # Suspended releases short-circuit ahead of the jitter sleep: there is
        # nothing to poll for, and paying up to a poll interval to learn that
        # would delay the peers sharing this pool for no reason.
        first = classify(
            initial_status, requested_version=request.version, workloads=None
        )
        if isinstance(first, Terminal) and first.verdict is Verdict.SKIPPED_SUSPENDED:
            record(state.transitions, state.ref, self.progress, first.phase, first.detail)
            return first.verdict, first.reason

        # Jittered start desynchronizes the pollers so N watchers don't hit
        # the apiserver in lockstep.
        self.sleep(self.rand(0.0, _POLL_INTERVAL_SEC))
        if cancel_event.is_set():
            return self._cancelled(state)

        # First pass reuses the status fetched during matching, saving one
        # kubectl call per HR.
        status: HelmReleaseStatus | None = initial_status
        while True:
            if status is not None:
                state.last_status = status
                terminal = self._evaluate(status, request, state, per_poll=per_poll)
                if terminal is not None:
                    return terminal

            if cancel_event.is_set():
                return self._cancelled(state)
            if self.clock() >= hr_deadline:
                # Which budget ran out changes what an operator should do:
                # raise --per-hr-timeout, or accept that the run as a whole
                # was too big for --total-timeout.
                reason = (
                    Reason.TOTAL_BUDGET_EXHAUSTED
                    if self.clock() >= total_deadline
                    else Reason.PER_HR_BUDGET_EXHAUSTED
                )
                # WARNING, not DEBUG: a tripped deadline is the single most
                # common non-obvious monitor outcome, and which of the two
                # budgets tripped is the whole content of the answer.
                _LOG.warning(
                    "monitor deadline reached: ns=%s name=%s reason=%s "
                    "per_hr=%gs total=%gs",
                    state.ref.namespace,
                    state.ref.name,
                    reason,
                    request.per_hr_timeout_seconds,
                    request.total_timeout_seconds,
                )
                record(state.transitions, state.ref, self.progress, "TimedOut", reason)
                return Verdict.TIMED_OUT, reason

            self.sleep(_POLL_INTERVAL_SEC)
            if cancel_event.is_set():
                return self._cancelled(state)

            polled = self._poll(state, per_poll=per_poll)
            if isinstance(polled, Terminal):
                return polled.verdict, polled.reason
            status = polled

    def _cancelled(self, state: _WatchState) -> tuple[Verdict, ReasonLike]:
        """Abandon this watch because a peer (or the total budget) cancelled the run.

        DEBUG, not WARNING: under `--fail-fast` every remaining watcher takes
        this path, so at INFO a 40-release run would bury the one outcome that
        actually explains the failure under 39 lines saying "and this one was
        stopped". The verdict still reaches the caller as an outcome.
        """
        _LOG.debug(
            "monitor watch cancelled: ns=%s name=%s",
            state.ref.namespace,
            state.ref.name,
        )
        reason = Reason.TOTAL_BUDGET_EXHAUSTED
        record(state.transitions, state.ref, self.progress, "Cancelled", reason)
        return Verdict.TIMED_OUT, reason

    def _evaluate(
        self,
        status: HelmReleaseStatus,
        request: MonitorRequest,
        state: _WatchState,
        *,
        per_poll: float,
    ) -> tuple[Verdict, ReasonLike] | None:
        """Classify one poll and record what it showed; non-None ends the watch.

        Two `classify` calls, not one: the first decides whether the workload
        rollout is even relevant yet, so we never pay a `list_owned_workloads`
        for a release the HelmRelease itself says is still reconciling.
        """
        decision = classify(
            status, requested_version=request.version, workloads=None
        )
        if isinstance(decision, Waiting) and decision.needs_workloads:
            workloads = self._list_workloads(state, per_poll=per_poll)
            if workloads is not None:
                state.last_workloads = workloads
                decision = classify(
                    status, requested_version=request.version, workloads=workloads
                )

        if isinstance(decision, Terminal):
            record(state.transitions, state.ref, self.progress, decision.phase, decision.detail)
            return decision.verdict, decision.reason

        if decision.signature != state.prev_signature:
            record(state.transitions, state.ref, self.progress, decision.phase, decision.detail)
            state.prev_signature = decision.signature
        return None

    def _poll(
        self, state: _WatchState, *, per_poll: float
    ) -> HelmReleaseStatus | Terminal | None:
        """Re-read the HR: a fresh status, a Terminal if it is gone, None if the read flaked.

        A NotFound is a real answer -- the release was deleted under us -- and
        must not be retried until the budget runs out, which is why it comes
        back as a Terminal rather than as another `None`.
        """
        try:
            return self.kubectl.get_helmrelease_status(state.ref, timeout=per_poll)
        except ExternalCommandError as exc:
            stderr = (exc.stderr or str(exc)).strip()
            if "NotFound" in stderr or "not found" in stderr:
                detail = stderr[:DETAIL_MAX]
                _LOG.error(
                    "HelmRelease disappeared while being watched: ns=%s name=%s: %s",
                    state.ref.namespace,
                    state.ref.name,
                    detail,
                )
                record(state.transitions, state.ref, self.progress, "Disappeared", detail)
                return Terminal(
                    verdict=Verdict.FAILED,
                    reason=Reason.DISAPPEARED,
                    phase="Disappeared",
                    detail=detail,
                )
            self._record_deduped(state, ("poll-error", stderr[:80]), "PollError", stderr)
            return None

    def _list_workloads(
        self, state: _WatchState, *, per_poll: float
    ) -> tuple[WorkloadRollout, ...] | None:
        """List owned workloads; None (plus a deduped transition) if the listing failed.

        Failing to read workloads is not failing the release: the HR may still
        converge, and the budget is what decides how long we keep asking.
        """
        try:
            return tuple(self.kubectl.list_owned_workloads(state.ref, timeout=per_poll))
        except ExternalCommandError as exc:
            stderr = (exc.stderr or str(exc)).strip()
            self._record_deduped(
                state,
                ("poll-error-workloads", stderr[:80]),
                "WorkloadsPollError",
                stderr,
            )
            return None

    def _record_deduped(
        self, state: _WatchState, signature: tuple[object, ...], phase: str, stderr: str
    ) -> None:
        """Record a transport-error transition unless the previous poll said the same thing."""
        if signature != state.prev_signature:
            # The dedupe is what makes this safe to log at WARNING from inside
            # the poll loop: a cluster that is unreachable for ten minutes
            # produces one line, not one per poll interval. A read that keeps
            # failing is why a release "just timed out" with no other symptom.
            _LOG.warning(
                "monitor poll degraded: ns=%s name=%s phase=%s: %s",
                state.ref.namespace,
                state.ref.name,
                phase,
                stderr[:DETAIL_MAX],
            )
            record(state.transitions, state.ref, self.progress, phase, stderr[:DETAIL_MAX])
            state.prev_signature = signature

    def _finalize(
        self,
        state: _WatchState,
        *,
        verdict: Verdict,
        reason: ReasonLike,
        per_poll: float,
        started_mono: float,
    ) -> MonitorOutcome:
        """Build the final MonitorOutcome, composing diagnostics unless the verdict is healthy.

        The elapsed time is taken before diagnostics run. Composing them
        issues a namespace-events call plus up to five workload-events calls,
        each bounded by the per-poll timeout -- so measuring afterwards
        folded up to ~a minute of log-scraping into `duration_seconds`, and
        only ever on the failure path. Failed promotions are precisely the
        ones whose duration we care about.
        """
        duration_seconds = self.clock() - started_mono
        diagnostics: str | None = None
        if not verdict.is_passing:
            # One line per failed release, carrying the pair (verdict, reason)
            # that the rendered report leads with. The report itself is not
            # logged: it is multi-kilobyte markdown, and the caller already
            # has it.
            _LOG.warning(
                "monitor outcome not ready: ns=%s name=%s verdict=%s reason=%s "
                "elapsed=%.1fs",
                state.ref.namespace,
                state.ref.name,
                verdict,
                reason,
                duration_seconds,
            )
            diagnostics = self._compose_diagnostics(
                state, verdict=verdict, reason=reason, per_poll=per_poll
            )
        return MonitorOutcome(
            ref=state.ref,
            verdict=verdict,
            reason=reason,
            last_status=state.last_status,
            last_workloads=state.last_workloads,
            transitions=tuple(state.transitions),
            diagnostics=diagnostics,
            duration_seconds=duration_seconds,
        )

    def _compose_diagnostics(
        self,
        state: _WatchState,
        *,
        verdict: Verdict,
        reason: ReasonLike,
        per_poll: float,
    ) -> str:
        """Render a markdown diagnostics report: status, workloads, transitions, events."""
        ref = state.ref
        parts: list[str] = [report.header(ref, verdict, reason)]

        last_status = state.last_status
        if last_status is not None:
            # "Stalled" is monitor's alone: it is the condition that ends a
            # watch, so its absence from the report would leave the verdict
            # unexplained.
            parts.extend(
                report.conditions(
                    last_status, ("Ready", "Released", "TestSuccess", "Stalled")
                )
            )
            parts.append(
                f"- desired: {last_status.desired_chart_name}@"
                f"{last_status.desired_chart_version}  "
                f"observed-gen: {last_status.observed_generation}/{last_status.generation}  "
                f"history[0]: {last_status.history_chart_version}"
            )

        if state.last_workloads:
            parts.append("\n### Workloads")
            for w in state.last_workloads:
                parts.append(
                    f"- {w.workload.kind}/{w.workload.namespace}/{w.workload.name}: "
                    f"converged={w.converged} "
                    f"(gen {w.observed_generation}/{w.generation}, "
                    f"ready={w.workload.ready}/{w.workload.desired}, "
                    f"available={w.workload.available}/{w.workload.desired})"
                )

        if state.transitions:
            parts.append("\n### Recent transitions")
            for t in state.transitions:
                parts.append(f"- {t.at.isoformat()} {t.phase} - {t.detail}")

        # Events come from where the workloads run, not where the HelmRelease
        # object lives; those differ whenever `spec.targetNamespace` is set.
        events_namespace = ref.target_namespace or ref.namespace
        if events_namespace:
            parts.append(f"\n### Events (namespace {events_namespace})")
            parts.append(
                report.capped_events(
                    self.kubectl.namespace_events(events_namespace, timeout=per_poll)
                )
            )

        not_converged = [w for w in state.last_workloads if not w.converged]
        if not_converged:
            parts.append("\n### Workload events")
            for w in not_converged[:_DIAGNOSTICS_WORKLOAD_CAP]:
                kind, ns, name = w.workload.kind, w.workload.namespace, w.workload.name
                parts.append(f"\n#### {kind}/{ns}/{name}")
                parts.append(
                    report.capped_events(
                        self.kubectl.workload_events(kind, ns, name, timeout=per_poll)
                    )
                )

        return "\n".join(parts)
