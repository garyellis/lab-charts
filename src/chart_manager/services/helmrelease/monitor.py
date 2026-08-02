"""Concurrent HelmRelease monitor.

Read-only service that fans out across matched Flux HelmReleases, verifies
HR Ready/Released + workload rollout under three-tier timeouts (per-poll,
per-HR, total), and aggregates per-HR outcomes for the caller. Service is
rendering-agnostic; callers (CLI, FastAPI) format MonitorResult themselves.
Caller owns kube context and concurrency bounds (default concurrency=4 to
be friendly to laptop EKS/GKE exec-auth caches; raise to 8 with care).
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

from chart_manager.integrations.helmrelease import (
    HelmReleaseClient,
    HelmReleaseRef,
    HelmReleaseStatus,
    WorkloadRollout,
)
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.duration import parse_duration
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.events.writer import EventWriter
from chart_manager.services.helmrelease import report
from chart_manager.services.helmrelease.classify import Terminal, Waiting, classify
from chart_manager.services.helmrelease.fanout import run_fanout, sorted_by_ref
from chart_manager.services.helmrelease.matching import filter_matched_statuses
from chart_manager.services.helmrelease.state import (
    DETAIL_MAX,
    NO_MATCH_REF,
    PASSING_VERDICTS,
    Reason,
    ReasonLike,
    Stage,
    Transition,
    Verdict,
    run_verdict,
)
from chart_manager.services.helmrelease.telemetry import PromotionTelemetry

_LOG = logging.getLogger(__name__)

_DIAGNOSTICS_WORKLOAD_CAP = 5


@dataclass(frozen=True)
class MonitorRequest:
    """Parameters for one monitor run; validates timeout ordering on init."""

    chart_name: str
    version: str
    namespace: str | None = None
    concurrency: int = 4
    per_poll_timeout: str = "10s"
    per_hr_timeout: str = "5m"
    total_timeout: str = "15m"
    poll_interval: float = 3.0
    recent_transitions_size: int = 5
    # When True, the first failed/timed-out outcome triggers cancellation of
    # remaining in-flight watchers; their outcomes carry `TotalBudgetExhausted`.
    fail_fast: bool = False
    # Which promotion target this rollout belongs to. None (the default, and
    # what an ad-hoc `helmrelease monitor` passes) means the run emits no
    # lifecycle events at all -- see services/helmrelease/telemetry.py.
    environment: str | None = None

    def __post_init__(self) -> None:
        """Reject empty identifiers and inconsistent timeout/interval bounds."""
        if not self.chart_name:
            raise ChartManagerError("chart_name must be non-empty")
        if not self.version:
            raise ChartManagerError("version must be non-empty")
        if self.concurrency < 1:
            raise ChartManagerError(f"concurrency must be >= 1 (got {self.concurrency})")
        if self.poll_interval <= 0:
            raise ChartManagerError(f"poll_interval must be > 0 (got {self.poll_interval})")
        if self.recent_transitions_size < 1:
            raise ChartManagerError(
                f"recent_transitions_size must be >= 1 (got {self.recent_transitions_size})"
            )
        per_hr = parse_duration(self.per_hr_timeout)
        if per_hr < self.poll_interval:
            raise ChartManagerError(
                f"per_hr_timeout ({self.per_hr_timeout}) must be >= poll_interval "
                f"({self.poll_interval}s)"
            )
        total = parse_duration(self.total_timeout)
        if total < per_hr:
            raise ChartManagerError(
                f"total_timeout ({self.total_timeout}) must be >= per_hr_timeout "
                f"({self.per_hr_timeout})"
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
    recent_transitions: tuple[Transition, ...]
    diagnostics: str | None
    duration_seconds: float


@dataclass(frozen=True)
class MonitorResult:
    """Aggregate of all watcher outcomes for a monitor run."""

    outcomes: tuple[MonitorOutcome, ...]
    total_duration_seconds: float
    total_timed_out: bool

    @property
    def ok(self) -> bool:
        """True when every outcome carries a passing verdict."""
        return bool(self.outcomes) and all(
            o.verdict in PASSING_VERDICTS for o in self.outcomes
        )

    @property
    def failures(self) -> tuple[MonitorOutcome, ...]:
        """Outcomes whose verdict is not a passing one."""
        return tuple(o for o in self.outcomes if o.verdict not in PASSING_VERDICTS)


@dataclass
class _WatchState:
    """Mutable per-HelmRelease state threaded through one watcher's phases.

    Mirrors `TestService._RunContext`: the polling loop, the classifier
    plumbing and the finalizer all need the same five values, and passing
    them positionally is how `_finalize` acquired an eight-keyword call
    repeated at eleven sites.
    """

    ref: HelmReleaseRef
    ring: deque[Transition]
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


class MonitorService:
    """Fans out one polling watcher per matched HelmRelease."""

    def __init__(
        self,
        client: HelmReleaseClient | None = None,
        *,
        kubectl: Kubectl | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        rand: Callable[[float, float], float] = random.uniform,
        progress: Callable[[HelmReleaseRef, Transition], None] | None = None,
        events: EventWriter | None = None,
        strict_events: bool = False,
    ) -> None:
        """Wire dependencies; sleep/clock/now/rand are injectable for tests."""
        self._client = client or HelmReleaseClient()
        # Two cluster adapters, not one: the HelmRelease queries are Flux
        # domain knowledge, the diagnostics events are plain kubectl. Both
        # must address the same cluster -- `Container` builds them from one
        # `Settings.kube_context`.
        self._kubectl = kubectl or Kubectl()
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._rand = rand
        self._progress = progress
        # Same non-fatal policy as PromoteService: the rollout has already
        # happened by the time these are written, so an unconfigured events
        # backend must not turn a converged run into a traceback. `strict`
        # is for callers where the event is the deliverable.
        self._events = events or EventWriter()
        self._strict_events = strict_events

    def monitor(self, request: MonitorRequest) -> MonitorResult:
        """Watch all matching HelmReleases concurrently and aggregate outcomes.

        Per-HR failures come back as outcomes; only infrastructure errors
        (kubectl/watcher crashes) raise, after cancelling peer watchers.
        """
        start = self._clock()
        per_poll = parse_duration(request.per_poll_timeout)
        matched = filter_matched_statuses(
            self._client,
            namespace=request.namespace,
            chart_name=request.chart_name,
            version=request.version,
            per_poll=per_poll,
        )

        _LOG.info(
            "monitor run started: chart=%s version=%s namespace=%s matched=%d "
            "concurrency=%d fail_fast=%s per_poll=%s per_hr=%s total=%s poll_interval=%.1fs",
            request.chart_name,
            request.version,
            request.namespace or "(all)",
            len(matched),
            request.concurrency,
            request.fail_fast,
            request.per_poll_timeout,
            request.per_hr_timeout,
            request.total_timeout,
            request.poll_interval,
        )

        if not matched:
            # A no-match is the one outcome that looks like success to every
            # caller reading `ok` and means nothing was watched at all.
            _LOG.warning(
                "monitor matched no HelmReleases: chart=%s version=%s namespace=%s",
                request.chart_name,
                request.version,
                request.namespace or "(all)",
            )
            elapsed = self._clock() - start
            return MonitorResult(
                outcomes=(
                    MonitorOutcome(
                        ref=NO_MATCH_REF,
                        verdict=Verdict.NO_MATCH,
                        reason=Reason.NO_HELMRELEASES_MATCHED,
                        last_status=None,
                        last_workloads=(),
                        recent_transitions=(),
                        diagnostics=None,
                        duration_seconds=elapsed,
                    ),
                ),
                total_duration_seconds=elapsed,
                total_timed_out=False,
            )

        # Emitted after the zero-match return: a run with nothing to watch
        # opened no interval, so bracketing it would put a WAITING_ROLLOUT on
        # the timeline that nothing will ever close.
        telemetry = PromotionTelemetry(
            writer=self._events,
            chart_name=request.chart_name,
            version=request.version,
            environment=request.environment,
            strict=self._strict_events,
        )
        telemetry.started(Stage.ROLLOUT, matched=len(matched))

        total_deadline = start + parse_duration(request.total_timeout)
        cancel_event = threading.Event()
        outcomes: list[MonitorOutcome] = []

        try:
            run_fanout(
                matched,
                concurrency=request.concurrency,
                clock=self._clock,
                total_deadline=total_deadline,
                cancel_event=cancel_event,
                outcomes=outcomes,
                work=lambda status: self._watch_one(
                    status, request, per_poll, total_deadline, cancel_event
                ),
                crash_label="monitor watcher",
                cancel_on=_fail_fast_predicate(request),
            )
        except Exception:
            _LOG.exception(
                "monitor run crashed: chart=%s version=%s matched=%d completed=%d",
                request.chart_name,
                request.version,
                len(matched),
                len(outcomes),
            )
            # An infrastructure failure still ends the interval opened above.
            # Without this the timeline keeps a WAITING_ROLLOUT that nothing
            # ever closes -- the exact defect this wiring exists to remove.
            #
            # `Exception`, not `BaseException`: Ctrl-C must kill a long
            # parallel run immediately, and this handler would put a network
            # write in front of the exit. An interrupted run genuinely has no
            # terminal state to report.
            telemetry.finished(
                Stage.ROLLOUT,
                Verdict.FAILED,
                total=len(matched),
                failures=len(matched) - len(outcomes),
            )
            raise

        elapsed = self._clock() - start
        result = MonitorResult(
            outcomes=sorted_by_ref(outcomes),
            total_duration_seconds=elapsed,
            total_timed_out=cancel_event.is_set(),
        )
        telemetry.finished(
            Stage.ROLLOUT,
            run_verdict((o.verdict for o in result.outcomes), success=Verdict.READY),
            total=len(result.outcomes),
            failures=len(result.failures),
        )
        _LOG.info(
            "monitor run finished: chart=%s version=%s outcomes=%d failures=%d "
            "cancelled=%s elapsed=%.1fs",
            request.chart_name,
            request.version,
            len(result.outcomes),
            len(result.failures),
            result.total_timed_out,
            elapsed,
        )
        return result

    def _watch_one(
        self,
        initial_status: HelmReleaseStatus,
        request: MonitorRequest,
        per_poll: float,
        total_deadline: float,
        cancel_event: threading.Event,
    ) -> MonitorOutcome:
        """Poll one HR until ready/failed/suspended or a deadline expires."""
        started_mono = self._clock()
        state = _WatchState(
            ref=initial_status.ref,
            ring=deque(maxlen=request.recent_transitions_size),
            last_status=initial_status,
        )
        verdict, reason = self._poll_until_terminal(
            initial_status,
            request,
            state,
            per_poll=per_poll,
            hr_deadline=min(
                started_mono + parse_duration(request.per_hr_timeout), total_deadline
            ),
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
            self._record(state, first.phase, first.detail)
            return first.verdict, first.reason

        # Jittered start desynchronizes the pollers so N watchers don't hit
        # the apiserver in lockstep.
        self._sleep(self._rand(0.0, request.poll_interval))
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
            if self._clock() >= hr_deadline:
                # Which budget ran out changes what an operator should do:
                # raise --per-hr-timeout, or accept that the run as a whole
                # was too big for --total-timeout.
                reason = (
                    Reason.TOTAL_BUDGET_EXHAUSTED
                    if self._clock() >= total_deadline
                    else Reason.PER_HR_BUDGET_EXHAUSTED
                )
                # WARNING, not DEBUG: a tripped deadline is the single most
                # common non-obvious monitor outcome, and which of the two
                # budgets tripped is the whole content of the answer.
                _LOG.warning(
                    "monitor deadline reached: ns=%s name=%s reason=%s "
                    "per_hr=%s total=%s",
                    state.ref.namespace,
                    state.ref.name,
                    reason,
                    request.per_hr_timeout,
                    request.total_timeout,
                )
                return Verdict.TIMED_OUT, reason

            self._sleep(request.poll_interval)
            if cancel_event.is_set():
                return self._cancelled(state)

            polled = self._poll(state, per_poll=per_poll)
            if isinstance(polled, Terminal):
                return polled.verdict, polled.reason
            status = polled

    @staticmethod
    def _cancelled(state: _WatchState) -> tuple[Verdict, ReasonLike]:
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
        return Verdict.TIMED_OUT, Reason.TOTAL_BUDGET_EXHAUSTED

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
            self._record(state, decision.phase, decision.detail)
            return decision.verdict, decision.reason

        if decision.signature != state.prev_signature:
            self._record(state, decision.phase, decision.detail)
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
            return self._client.get_status(state.ref, timeout=per_poll)
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
                self._record(state, "Disappeared", detail)
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
            return tuple(self._client.list_owned_workloads(state.ref, timeout=per_poll))
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
            self._record(state, phase, stderr[:DETAIL_MAX])
            state.prev_signature = signature

    def _record(self, state: _WatchState, phase: str, detail: str) -> None:
        """Append a transition to the ring buffer and fire the progress callback."""
        transition = Transition(at=self._now(), phase=phase, detail=detail)
        state.ring.append(transition)
        self._fire_progress(state.ref, transition)

    def _fire_progress(self, ref: HelmReleaseRef, transition: Transition) -> None:
        """Invoke the progress callback if set; swallow+log any exception it raises."""
        if self._progress is None:
            return
        try:
            self._progress(ref, transition)
        except Exception:
            _LOG.exception("monitor progress callback raised")

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
        duration_seconds = self._clock() - started_mono
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
            recent_transitions=tuple(state.ring),
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

        if state.ring:
            parts.append("\n### Recent transitions")
            for t in state.ring:
                parts.append(f"- {t.at.isoformat()} {t.phase} - {t.detail}")

        # Events come from where the workloads run, not where the HelmRelease
        # object lives. Those differ whenever `spec.targetNamespace` is set,
        # and this used `ref.namespace` -- reporting events from a namespace
        # containing none of the resources listed above. TestService already
        # keys on target_namespace; this matches it.
        events_namespace = ref.target_namespace or ref.namespace
        if events_namespace:
            parts.append(f"\n### Events (namespace {events_namespace})")
            parts.append(
                report.safe_events(
                    partial(
                        self._kubectl.namespace_events,
                        events_namespace,
                        timeout=per_poll,
                    )
                )
            )

        not_converged = [w for w in state.last_workloads if not w.converged]
        if not_converged:
            parts.append("\n### Workload events")
            for w in not_converged[:_DIAGNOSTICS_WORKLOAD_CAP]:
                kind, ns, name = w.workload.kind, w.workload.namespace, w.workload.name
                parts.append(f"\n#### {kind}/{ns}/{name}")
                parts.append(
                    report.safe_events(
                        # `partial`, not a closure: binding the loop's values
                        # now means the callable cannot depend on when
                        # `safe_events` gets around to invoking it.
                        partial(
                            self._kubectl.workload_events,
                            kind,
                            ns,
                            name,
                            timeout=per_poll,
                        )
                    )
                )

        return "\n".join(parts)
