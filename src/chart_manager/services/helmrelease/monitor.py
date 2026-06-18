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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from chart_manager.integrations.flux import (
    Flux,
    HelmReleaseRef,
    HelmReleaseStatus,
    WorkloadRollout,
)
from chart_manager.plumbing.duration import parse_duration
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.helmrelease._common import (
    NO_MATCH_REF,
    Transition,
    filter_matched_statuses,
    truncate_lines,
)

_LOG = logging.getLogger(__name__)

Verdict = Literal["ready", "failed", "timed-out", "skipped-suspended", "no-match"]

_TERMINAL_READY_REASONS = frozenset({
    "InstallFailed",
    "UpgradeFailed",
    "ReconciliationFailed",
    "ArtifactFailed",
    "RetryExhausted",
})

_DETAIL_MAX = 200
_DIAGNOSTICS_WORKLOAD_CAP = 5
_EVENTS_LINE_CAP = 80


@dataclass(frozen=True)
class MonitorRequest:
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

    def __post_init__(self) -> None:
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
    ref: HelmReleaseRef
    verdict: Verdict
    reason: str
    last_status: HelmReleaseStatus | None
    last_workloads: tuple[WorkloadRollout, ...]
    recent_transitions: tuple[Transition, ...]
    diagnostics: str | None
    duration_seconds: float


@dataclass(frozen=True)
class MonitorResult:
    outcomes: tuple[MonitorOutcome, ...]
    total_duration_seconds: float
    total_timed_out: bool

    @property
    def ok(self) -> bool:
        return bool(self.outcomes) and all(
            o.verdict in ("ready", "skipped-suspended") for o in self.outcomes
        )

    @property
    def failures(self) -> tuple[MonitorOutcome, ...]:
        return tuple(
            o for o in self.outcomes if o.verdict not in ("ready", "skipped-suspended")
        )


class MonitorService:
    def __init__(
        self,
        flux: Flux | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        rand: Callable[[float, float], float] = random.uniform,
        progress: Callable[[HelmReleaseRef, Transition], None] | None = None,
    ) -> None:
        self._flux = flux or Flux()
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._rand = rand
        self._progress = progress

    def monitor(self, request: MonitorRequest) -> MonitorResult:
        start = self._clock()
        per_poll = parse_duration(request.per_poll_timeout)
        matched = filter_matched_statuses(
            self._flux,
            namespace=request.namespace,
            chart_name=request.chart_name,
            version=request.version,
            per_poll=per_poll,
        )

        if not matched:
            elapsed = self._clock() - start
            return MonitorResult(
                outcomes=(
                    MonitorOutcome(
                        ref=NO_MATCH_REF,
                        verdict="no-match",
                        reason="NoHelmReleasesMatched",
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

        total_deadline = start + parse_duration(request.total_timeout)
        cancel_event = threading.Event()
        outcomes: list[MonitorOutcome] = []

        with ThreadPoolExecutor(max_workers=request.concurrency) as ex:
            futures = [
                ex.submit(self._watch_one, status, request, total_deadline, cancel_event)
                for status in matched
            ]
            for fut in as_completed(futures):
                try:
                    outcome = fut.result()
                except ExternalCommandError:
                    cancel_event.set()
                    raise
                except ChartManagerError:
                    cancel_event.set()
                    raise
                except BaseException as exc:
                    cancel_event.set()
                    raise ChartManagerError(
                        f"monitor watcher crashed: {exc!r}"
                    ) from exc
                outcomes.append(outcome)
                if request.fail_fast and outcome.verdict in ("failed", "timed-out"):
                    cancel_event.set()
                if self._clock() >= total_deadline:
                    cancel_event.set()

        outcomes.sort(key=lambda o: (o.ref.namespace, o.ref.name))
        elapsed = self._clock() - start
        return MonitorResult(
            outcomes=tuple(outcomes),
            total_duration_seconds=elapsed,
            total_timed_out=cancel_event.is_set(),
        )

    def _watch_one(
        self,
        initial_status: HelmReleaseStatus,
        request: MonitorRequest,
        total_deadline: float,
        cancel_event: threading.Event,
    ) -> MonitorOutcome:
        ref = initial_status.ref
        per_poll = parse_duration(request.per_poll_timeout)
        wait_start_mono = self._clock()
        hr_deadline = min(
            wait_start_mono + parse_duration(request.per_hr_timeout), total_deadline
        )
        ring: deque[Transition] = deque(maxlen=request.recent_transitions_size)
        last_status: HelmReleaseStatus = initial_status
        last_workloads: tuple[WorkloadRollout, ...] = ()
        prev_signature: object = None

        if initial_status.suspended:
            self._record(
                ring, ref, "Suspended", "HR spec.suspend=true", wait_start_mono
            )
            return self._finalize(
                ref=ref,
                verdict="skipped-suspended",
                reason="Suspended",
                last_status=last_status,
                last_workloads=last_workloads,
                ring=ring,
                request=request,
                started_mono=wait_start_mono,
            )

        self._sleep(self._rand(0.0, request.poll_interval))
        if cancel_event.is_set():
            return self._finalize(
                ref=ref,
                verdict="timed-out",
                reason="TotalBudgetExhausted",
                last_status=last_status,
                last_workloads=last_workloads,
                ring=ring,
                request=request,
                started_mono=wait_start_mono,
            )

        first_iteration = True
        while True:
            status: HelmReleaseStatus | None
            if first_iteration:
                status = initial_status
                first_iteration = False
            else:
                try:
                    status = self._flux.get_status(ref, timeout=per_poll)
                except ExternalCommandError as exc:
                    stderr = (exc.stderr or str(exc)).strip()
                    if "NotFound" in stderr or "not found" in stderr:
                        self._record(
                            ring, ref, "Disappeared", stderr[:_DETAIL_MAX], wait_start_mono
                        )
                        return self._finalize(
                            ref=ref,
                            verdict="failed",
                            reason="Disappeared",
                            last_status=last_status,
                            last_workloads=last_workloads,
                            ring=ring,
                            request=request,
                            started_mono=wait_start_mono,
                        )
                    sig = ("poll-error", stderr[:80])
                    if sig != prev_signature:
                        self._record(
                            ring, ref, "PollError", stderr[:_DETAIL_MAX], wait_start_mono
                        )
                        prev_signature = sig
                    status = None

            if status is not None:
                last_status = status
                if status.suspended:
                    self._record(
                        ring, ref, "Suspended", "HR spec.suspend=true", wait_start_mono
                    )
                    return self._finalize(
                        ref=ref,
                        verdict="skipped-suspended",
                        reason="Suspended",
                        last_status=last_status,
                        last_workloads=last_workloads,
                        ring=ring,
                        request=request,
                        started_mono=wait_start_mono,
                    )

                stalled = status.condition("Stalled")
                if stalled is not None and stalled.status == "True":
                    self._record(
                        ring, ref, "Stalled", stalled.message[:_DETAIL_MAX], wait_start_mono
                    )
                    return self._finalize(
                        ref=ref,
                        verdict="failed",
                        reason="Stalled",
                        last_status=last_status,
                        last_workloads=last_workloads,
                        ring=ring,
                        request=request,
                        started_mono=wait_start_mono,
                    )

                ready_cond = status.ready
                if (
                    ready_cond is not None
                    and ready_cond.status == "False"
                    and ready_cond.reason in _TERMINAL_READY_REASONS
                ):
                    self._record(
                        ring,
                        ref,
                        f"Ready=False:{ready_cond.reason}",
                        ready_cond.message[:_DETAIL_MAX],
                        wait_start_mono,
                    )
                    return self._finalize(
                        ref=ref,
                        verdict="failed",
                        reason=ready_cond.reason,
                        last_status=last_status,
                        last_workloads=last_workloads,
                        ring=ring,
                        request=request,
                        started_mono=wait_start_mono,
                    )

                # TestSuccess=False is only terminal once Released=True; before
                # that it just reflects the pre-run state of the test hook.
                test_cond = status.test_success
                released_cond = status.released
                if (
                    test_cond is not None
                    and test_cond.status == "False"
                    and released_cond is not None
                    and released_cond.status == "True"
                ):
                    self._record(
                        ring,
                        ref,
                        "TestSuccess=False",
                        test_cond.message[:_DETAIL_MAX],
                        wait_start_mono,
                    )
                    return self._finalize(
                        ref=ref,
                        verdict="failed",
                        reason=test_cond.reason or "TestFailed",
                        last_status=last_status,
                        last_workloads=last_workloads,
                        ring=ring,
                        request=request,
                        started_mono=wait_start_mono,
                    )

                ready_status = ready_cond.status if ready_cond else "Unknown"
                ready_reason = ready_cond.reason if ready_cond else ""
                gen_caught_up = status.observed_generation == status.generation
                history_matches = status.history_chart_version == request.version
                ready_true = ready_cond is not None and ready_cond.status == "True"

                not_converged_names: tuple[str, ...] = ()
                if gen_caught_up and history_matches and ready_true:
                    try:
                        workloads = tuple(
                            self._flux.list_owned_workloads(ref, timeout=per_poll)
                        )
                    except ExternalCommandError as exc:
                        stderr = (exc.stderr or str(exc)).strip()
                        sig = ("poll-error-workloads", stderr[:80])
                        if sig != prev_signature:
                            self._record(
                                ring,
                                ref,
                                "WorkloadsPollError",
                                stderr[:_DETAIL_MAX],
                                wait_start_mono,
                            )
                            prev_signature = sig
                    else:
                        last_workloads = workloads
                        not_converged = tuple(w for w in workloads if not w.converged)
                        not_converged_names = tuple(
                            sorted(
                                f"{w.workload.kind}/{w.workload.namespace}/{w.workload.name}"
                                for w in not_converged
                            )
                        )
                        if not not_converged:
                            self._record(
                                ring,
                                ref,
                                "Ready",
                                "HR Ready=True and all workloads converged",
                                wait_start_mono,
                            )
                            return self._finalize(
                                ref=ref,
                                verdict="ready",
                                reason="Ready",
                                last_status=last_status,
                                last_workloads=last_workloads,
                                ring=ring,
                                request=request,
                                started_mono=wait_start_mono,
                            )

                signature = (
                    ready_status,
                    ready_reason,
                    status.observed_generation,
                    frozenset(not_converged_names),
                    status.suspended,
                )
                if signature != prev_signature:
                    phase = _phase_label(
                        ready_status,
                        ready_reason,
                        gen_caught_up,
                        history_matches,
                        not_converged_names,
                    )
                    detail = _phase_detail(status, request.version, not_converged_names)
                    self._record(ring, ref, phase, detail, wait_start_mono)
                    prev_signature = signature

            if cancel_event.is_set():
                return self._finalize(
                    ref=ref,
                    verdict="timed-out",
                    reason="TotalBudgetExhausted",
                    last_status=last_status,
                    last_workloads=last_workloads,
                    ring=ring,
                    request=request,
                    started_mono=wait_start_mono,
                )
            if self._clock() >= hr_deadline:
                reason = (
                    "TotalBudgetExhausted"
                    if self._clock() >= total_deadline
                    else "PerHRBudgetExhausted"
                )
                return self._finalize(
                    ref=ref,
                    verdict="timed-out",
                    reason=reason,
                    last_status=last_status,
                    last_workloads=last_workloads,
                    ring=ring,
                    request=request,
                    started_mono=wait_start_mono,
                )

            self._sleep(request.poll_interval)
            if cancel_event.is_set():
                return self._finalize(
                    ref=ref,
                    verdict="timed-out",
                    reason="TotalBudgetExhausted",
                    last_status=last_status,
                    last_workloads=last_workloads,
                    ring=ring,
                    request=request,
                    started_mono=wait_start_mono,
                )

    def _record(
        self,
        ring: deque[Transition],
        ref: HelmReleaseRef,
        phase: str,
        detail: str,
        started_mono: float,
    ) -> None:
        transition = Transition(at=self._now(), phase=phase, detail=detail)
        ring.append(transition)
        self._fire_progress(ref, transition)

    def _fire_progress(self, ref: HelmReleaseRef, transition: Transition) -> None:
        if self._progress is None:
            return
        try:
            self._progress(ref, transition)
        except Exception:
            _LOG.exception("monitor progress callback raised")

    def _finalize(
        self,
        *,
        ref: HelmReleaseRef,
        verdict: Verdict,
        reason: str,
        last_status: HelmReleaseStatus | None,
        last_workloads: tuple[WorkloadRollout, ...],
        ring: deque[Transition],
        request: MonitorRequest,
        started_mono: float,
    ) -> MonitorOutcome:
        diagnostics: str | None = None
        if verdict not in ("ready", "skipped-suspended"):
            diagnostics = self._compose_diagnostics(
                ref=ref,
                verdict=verdict,
                reason=reason,
                last_status=last_status,
                last_workloads=last_workloads,
                ring=tuple(ring),
                per_poll=parse_duration(request.per_poll_timeout),
            )
        return MonitorOutcome(
            ref=ref,
            verdict=verdict,
            reason=reason,
            last_status=last_status,
            last_workloads=last_workloads,
            recent_transitions=tuple(ring),
            diagnostics=diagnostics,
            duration_seconds=self._clock() - started_mono,
        )

    def _compose_diagnostics(
        self,
        *,
        ref: HelmReleaseRef,
        verdict: Verdict,
        reason: str,
        last_status: HelmReleaseStatus | None,
        last_workloads: tuple[WorkloadRollout, ...],
        ring: tuple[Transition, ...],
        per_poll: float,
    ) -> str:
        ns = ref.namespace or "(none)"
        name = ref.name or "(none)"
        parts: list[str] = [f"## {ns}/{name} - {verdict}: {reason}"]

        if last_status is not None:
            parts.append("\n### Status")
            for cond_type in ("Ready", "Released", "TestSuccess", "Stalled"):
                cond = last_status.condition(cond_type)
                if cond is None:
                    parts.append(f"- {cond_type}: (absent)")
                else:
                    parts.append(
                        f"- {cond_type}: {cond.status} ({cond.reason}) - {cond.message}"
                    )
            parts.append(
                f"- desired: {last_status.desired_chart_name}@"
                f"{last_status.desired_chart_version}  "
                f"observed-gen: {last_status.observed_generation}/{last_status.generation}  "
                f"history[0]: {last_status.history_chart_version}"
            )

        if last_workloads:
            parts.append("\n### Workloads")
            for w in last_workloads:
                parts.append(
                    f"- {w.workload.kind}/{w.workload.namespace}/{w.workload.name}: "
                    f"converged={w.converged} "
                    f"(gen {w.observed_generation}/{w.generation}, "
                    f"ready={w.workload.ready}/{w.workload.desired}, "
                    f"available={w.workload.available}/{w.workload.desired})"
                )

        if ring:
            parts.append("\n### Recent transitions")
            for t in ring:
                parts.append(f"- {t.at.isoformat()} {t.phase} - {t.detail}")

        if ref.namespace:
            parts.append(f"\n### Events (namespace {ref.namespace})")
            parts.append(self._safe_events(self._flux.namespace_events, ref.namespace, per_poll))

        not_converged = [w for w in last_workloads if not w.converged][:_DIAGNOSTICS_WORKLOAD_CAP]
        if not_converged:
            parts.append("\n### Workload events")
            for w in not_converged:
                parts.append(
                    f"\n#### {w.workload.kind}/{w.workload.namespace}/{w.workload.name}"
                )
                parts.append(
                    self._safe_workload_events(
                        w.workload.kind, w.workload.namespace, w.workload.name, per_poll
                    )
                )

        return "\n".join(parts)

    def _safe_events(
        self,
        fetch: Callable[..., str],
        namespace: str,
        per_poll: float,
    ) -> str:
        try:
            blob = fetch(namespace, timeout=per_poll)
        except ExternalCommandError as exc:
            stderr = (exc.stderr or str(exc)).strip()
            return f"Events: (unavailable: {stderr[:_DETAIL_MAX]})"
        return truncate_lines(blob, _EVENTS_LINE_CAP)

    def _safe_workload_events(
        self, kind: str, namespace: str, name: str, per_poll: float
    ) -> str:
        try:
            blob = self._flux.workload_events(kind, namespace, name, timeout=per_poll)
        except ExternalCommandError as exc:
            stderr = (exc.stderr or str(exc)).strip()
            return f"Events: (unavailable: {stderr[:_DETAIL_MAX]})"
        return truncate_lines(blob, _EVENTS_LINE_CAP)


def _phase_label(
    ready_status: str,
    ready_reason: str,
    gen_caught_up: bool,
    history_matches: bool,
    not_converged_names: tuple[str, ...],
) -> str:
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
    return detail[:_DETAIL_MAX]
