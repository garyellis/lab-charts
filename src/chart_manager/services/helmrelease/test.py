"""Concurrent `helm test` runner for matched Flux HelmReleases.

Read-mostly (deletes only stale test pods on the cluster; never mutates HR
specs). Caller owns kube context. Fan-out is bounded by `concurrency`;
each `helm test` invocation creates test pods on the cluster -- tune
`concurrency` for small clusters. Service is rendering-agnostic; callers
format `TestResult`.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar, Literal

from chart_manager.integrations.flux import Flux, HelmReleaseRef, HelmReleaseStatus
from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.commands import CommandResult
from chart_manager.plumbing.duration import parse_duration
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.helmrelease._common import (
    NO_MATCH_REF,
    Transition,
    filter_matched_statuses,
    truncate_bytes,
    truncate_lines,
)

_LOG = logging.getLogger(__name__)

TestVerdict = Literal[
    "passed",
    "failed",
    "timed-out",
    "skipped-not-ready",
    "skipped-suspended",
    "no-match",
]

# Pod phases that mean a previous helm-test run is still live; we MUST NOT
# run helm again (helm would recreate-conflict or, worse, kill the live
# pod). Empty phase means the kubelet hasn't reported yet -- treat the
# same as Pending so we don't race the apiserver.
_IN_FLIGHT_PHASES = frozenset({"Pending", "Running", "Unknown", ""})
_STALE_PHASES = frozenset({"Succeeded", "Failed"})

_PHASE_LOG_MAX = 5
_EVENTS_LINE_CAP = 80
_PASSED_VERDICTS: frozenset[TestVerdict] = frozenset({"passed", "skipped-suspended"})

_NO_TESTS_PATTERN = re.compile(r"no tests (to run|for chart|found)", re.IGNORECASE)
_HELM_UNAVAILABLE_PATTERN = re.compile(
    r"cluster unreachable|connection refused|INSTALLATION FAILED",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TestRequest:
    # Tell pytest to skip collection of this Test*-named class.
    __test__: ClassVar[bool] = False

    chart_name: str
    version: str
    namespace: str | None = None
    concurrency: int = 4
    per_poll_timeout: str = "10s"
    # per_hr_timeout: per-pod readiness wait passed to helm `--timeout`.
    # Charts with multiple test hooks may exceed this wall-clock; the
    # subprocess cap (per_hr + subprocess_slack, bounded by total) is the
    # hard stop.
    per_hr_timeout: str = "5m"
    total_timeout: str = "15m"
    subprocess_slack: str = "30s"
    pod_log_tail: int = 200
    pod_log_max_bytes: int = 16_384
    diagnostics_pod_cap: int = 5
    helm_test_stdout_max_bytes: int = 32_768
    # concurrency: each helm test creates 1+ test pods. concurrency=4
    # against 4 HRs with multi-pod suites may create 8-16 pods concurrently
    # on the cluster; tune down on small clusters.

    def __post_init__(self) -> None:
        if not self.chart_name:
            raise ChartManagerError("chart_name must be non-empty")
        if not self.version:
            raise ChartManagerError("version must be non-empty")
        if self.concurrency < 1:
            raise ChartManagerError(f"concurrency must be >= 1 (got {self.concurrency})")
        if self.pod_log_tail < 1:
            raise ChartManagerError(f"pod_log_tail must be >= 1 (got {self.pod_log_tail})")
        if self.pod_log_max_bytes < 256:
            raise ChartManagerError(
                f"pod_log_max_bytes must be >= 256 (got {self.pod_log_max_bytes})"
            )
        if self.diagnostics_pod_cap < 1:
            raise ChartManagerError(
                f"diagnostics_pod_cap must be >= 1 (got {self.diagnostics_pod_cap})"
            )
        if self.helm_test_stdout_max_bytes < 256:
            raise ChartManagerError(
                f"helm_test_stdout_max_bytes must be >= 256 "
                f"(got {self.helm_test_stdout_max_bytes})"
            )
        per_hr = parse_duration(self.per_hr_timeout)
        if per_hr < 30.0:
            raise ChartManagerError(
                f"per_hr_timeout ({self.per_hr_timeout}) must be >= 30s"
            )
        total = parse_duration(self.total_timeout)
        if total < per_hr:
            raise ChartManagerError(
                f"total_timeout ({self.total_timeout}) must be >= per_hr_timeout "
                f"({self.per_hr_timeout})"
            )
        slack = parse_duration(self.subprocess_slack)
        if slack < 5.0:
            raise ChartManagerError(
                f"subprocess_slack ({self.subprocess_slack}) must be >= 5s"
            )


@dataclass(frozen=True)
class TestPodSnapshot:
    __test__: ClassVar[bool] = False

    namespace: str
    name: str
    phase: str
    logs: str
    previous_logs: str | None


@dataclass(frozen=True)
class TestOutcome:
    __test__: ClassVar[bool] = False

    ref: HelmReleaseRef
    verdict: TestVerdict
    reason: str
    helm_test_returncode: int | None
    helm_test_stdout: str | None
    helm_test_stderr: str | None
    test_pods: tuple[TestPodSnapshot, ...]
    last_status: HelmReleaseStatus | None
    phase_log: tuple[Transition, ...]
    diagnostics: str | None
    duration_seconds: float


@dataclass(frozen=True)
class TestResult:
    __test__: ClassVar[bool] = False

    outcomes: tuple[TestOutcome, ...]
    total_duration_seconds: float
    total_timed_out: bool

    @property
    def ok(self) -> bool:
        return bool(self.outcomes) and all(
            o.verdict in _PASSED_VERDICTS for o in self.outcomes
        )

    @property
    def failures(self) -> tuple[TestOutcome, ...]:
        return tuple(o for o in self.outcomes if o.verdict not in _PASSED_VERDICTS)


@dataclass
class _ParsedRequest:
    per_poll_sec: float
    per_hr_sec: float
    total_sec: float
    subprocess_slack_sec: float


# Internal aggregate for a single watcher; lets us thread state through
# the phase methods without dragging 8 positional args.
@dataclass
class _RunContext:
    ref: HelmReleaseRef
    initial_status: HelmReleaseStatus
    parsed: _ParsedRequest
    request: TestRequest
    started_mono: float
    total_deadline: float
    cancel_event: threading.Event
    phase_log: list[Transition] = field(default_factory=list)


class TestService:
    __test__ = False

    def __init__(
        self,
        flux: Flux | None = None,
        helm: Helm | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        progress: Callable[[HelmReleaseRef, Transition], None] | None = None,
    ) -> None:
        self._flux = flux or Flux()
        # verbose=False prevents 4 concurrent helm test stdout streams from
        # interleaving into garbage; the service captures and returns
        # stdout/stderr on the result instead.
        self._helm = helm or Helm(verbose=False)
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._progress = progress

    def test(self, request: TestRequest) -> TestResult:
        start = self._clock()
        parsed = _ParsedRequest(
            per_poll_sec=parse_duration(request.per_poll_timeout),
            per_hr_sec=parse_duration(request.per_hr_timeout),
            total_sec=parse_duration(request.total_timeout),
            subprocess_slack_sec=parse_duration(request.subprocess_slack),
        )

        matched = filter_matched_statuses(
            self._flux,
            namespace=request.namespace,
            chart_name=request.chart_name,
            version=request.version,
            per_poll=parsed.per_poll_sec,
        )

        if not matched:
            elapsed = self._clock() - start
            return TestResult(
                outcomes=(
                    TestOutcome(
                        ref=NO_MATCH_REF,
                        verdict="no-match",
                        reason="NoHelmReleasesMatched",
                        helm_test_returncode=None,
                        helm_test_stdout=None,
                        helm_test_stderr=None,
                        test_pods=(),
                        last_status=None,
                        phase_log=(),
                        diagnostics=None,
                        duration_seconds=elapsed,
                    ),
                ),
                total_duration_seconds=elapsed,
                total_timed_out=False,
            )

        total_deadline = start + parsed.total_sec
        cancel_event = threading.Event()
        outcomes: list[TestOutcome] = []

        with ThreadPoolExecutor(max_workers=request.concurrency) as ex:
            futures = [
                ex.submit(
                    self._test_one, status, parsed, request, total_deadline, cancel_event
                )
                for status in matched
            ]
            for fut in as_completed(futures):
                try:
                    outcomes.append(fut.result())
                except ExternalCommandError:
                    cancel_event.set()
                    raise
                except ChartManagerError:
                    cancel_event.set()
                    raise
                except BaseException as exc:
                    cancel_event.set()
                    raise ChartManagerError(
                        f"test watcher crashed: {exc!r}"
                    ) from exc
                if self._clock() >= total_deadline:
                    cancel_event.set()

        outcomes.sort(key=lambda o: (o.ref.namespace, o.ref.name))
        elapsed = self._clock() - start
        return TestResult(
            outcomes=tuple(outcomes),
            total_duration_seconds=elapsed,
            total_timed_out=cancel_event.is_set(),
        )

    # --- per-HR pipeline ---------------------------------------------------

    def _test_one(
        self,
        initial_status: HelmReleaseStatus,
        parsed: _ParsedRequest,
        request: TestRequest,
        total_deadline: float,
        cancel_event: threading.Event,
    ) -> TestOutcome:
        ctx = _RunContext(
            ref=initial_status.ref,
            initial_status=initial_status,
            parsed=parsed,
            request=request,
            started_mono=self._clock(),
            total_deadline=total_deadline,
            cancel_event=cancel_event,
        )
        self._fire(ctx, "Preflight", f"chart={request.chart_name}@{request.version}")

        preflight = self._preflight(ctx)
        if preflight is not None:
            return preflight

        reap = self._reap(ctx)
        if reap is not None:
            return reap

        if ctx.cancel_event.is_set() or self._clock() >= ctx.total_deadline:
            return self._finalize_timed_out(ctx, "TotalBudgetExhausted")

        return self._run_helm(ctx)

    def _preflight(self, ctx: _RunContext) -> TestOutcome | None:
        s = ctx.initial_status
        if s.suspended:
            return self._finalize(
                ctx,
                verdict="skipped-suspended",
                reason="Suspended",
                last_status=s,
            )
        released = s.released
        if released is None or released.status != "True":
            return self._finalize(
                ctx,
                verdict="skipped-not-ready",
                reason="NotReleased",
                last_status=s,
            )
        if s.observed_generation != s.generation:
            return self._finalize(
                ctx,
                verdict="skipped-not-ready",
                reason="GenerationLag",
                last_status=s,
            )
        if ctx.cancel_event.is_set() or self._clock() >= ctx.total_deadline:
            return self._finalize_timed_out(ctx, "TotalBudgetExhausted")
        return None

    def _reap(self, ctx: _RunContext) -> TestOutcome | None:
        self._fire(ctx, "Reaping", "checking for existing test pods")
        try:
            pods = self._flux.list_test_pods(ctx.ref, timeout=ctx.parsed.per_poll_sec)
        except ExternalCommandError as exc:
            return self._finalize(
                ctx,
                verdict="failed",
                reason="ReapListFailed",
                last_status=ctx.initial_status,
                inline_diagnostics=str(exc),
            )

        in_flight = [p for p in pods if p[2] in _IN_FLIGHT_PHASES]
        if in_flight:
            return self._finalize(
                ctx,
                verdict="failed",
                reason="TestPodInFlight",
                last_status=ctx.initial_status,
                in_flight=tuple(in_flight),
            )

        residual: list[str] = []
        for ns, name, _phase in [p for p in pods if p[2] in _STALE_PHASES]:
            try:
                self._flux.delete_pod(ns, name, timeout=ctx.parsed.per_poll_sec)
            except ExternalCommandError:
                residual.append(f"{ns}/{name}")
        if residual:
            return self._finalize(
                ctx,
                verdict="failed",
                reason="ReapIncomplete",
                last_status=ctx.initial_status,
                residual=tuple(residual),
            )
        return None

    def _run_helm(self, ctx: _RunContext) -> TestOutcome:
        self._fire(
            ctx,
            "Running",
            f"helm test {ctx.ref.release_name} -n {ctx.ref.storage_namespace}",
        )
        # The subprocess cap is bounded by the total deadline so a runaway
        # helm test can't outlive the global budget even if its own
        # --timeout claims another N minutes.
        remaining_total = max(0.0, ctx.total_deadline - self._clock())
        subprocess_cap = min(
            ctx.parsed.per_hr_sec + ctx.parsed.subprocess_slack_sec, remaining_total
        )
        if subprocess_cap <= 0:
            return self._finalize_timed_out(ctx, "TotalBudgetExhausted")

        try:
            result = self._helm.test(
                ctx.ref.release_name,
                namespace=ctx.ref.storage_namespace,
                timeout=ctx.request.per_hr_timeout,
                logs=True,
                subprocess_timeout=subprocess_cap,
            )
        except ExternalCommandError as exc:
            msg = str(exc)
            if "timed out" in msg:
                reason = (
                    "TotalBudgetExhausted"
                    if self._clock() >= ctx.total_deadline
                    else "PerHRBudgetExhausted"
                )
                return self._finalize_timed_out(ctx, reason)
            # Defensive: with check=False the runner shouldn't raise on
            # rc != 0, but propagate any other surprise as HelmUnavailable
            # so we still produce a structured outcome.
            return self._finalize(
                ctx,
                verdict="failed",
                reason="HelmUnavailable",
                last_status=ctx.initial_status,
                helm_result=None,
                inline_diagnostics=msg,
            )

        return self._classify(ctx, result)

    def _classify(self, ctx: _RunContext, result: CommandResult) -> TestOutcome:
        stderr = result.stderr or ""
        rc = result.returncode

        if rc == 0:
            self._fire(ctx, "Finished", "passed")
            return self._finalize(
                ctx,
                verdict="passed",
                reason="AllTestsPassed",
                last_status=ctx.initial_status,
                helm_result=result,
            )

        # Charts with no `helm.sh/hook=test` templates report rc != 0 with
        # a stderr line matching one of these phrasings. Treat as passed,
        # no diagnostics, no Flux event calls.
        if _NO_TESTS_PATTERN.search(stderr):
            self._fire(ctx, "Finished", "no tests defined")
            return self._finalize(
                ctx,
                verdict="passed",
                reason="NoTestsDefined",
                last_status=ctx.initial_status,
                helm_result=result,
            )

        if _HELM_UNAVAILABLE_PATTERN.search(stderr):
            self._fire(ctx, "Finished", "helm unavailable")
            return self._finalize(
                ctx,
                verdict="failed",
                reason="HelmUnavailable",
                last_status=ctx.initial_status,
                helm_result=result,
            )

        if "already exists" in stderr.lower():
            self._fire(ctx, "Finished", "test pod conflict")
            return self._finalize(
                ctx,
                verdict="failed",
                reason="TestPodConflict",
                last_status=ctx.initial_status,
                helm_result=result,
            )

        self._fire(ctx, "Finished", f"failed (rc={rc})")
        return self._finalize(
            ctx,
            verdict="failed",
            reason="TestFailed",
            last_status=ctx.initial_status,
            helm_result=result,
        )

    # --- finalize / diagnostics -------------------------------------------

    def _finalize_timed_out(self, ctx: _RunContext, reason: str) -> TestOutcome:
        return self._finalize(
            ctx,
            verdict="timed-out",
            reason=reason,
            last_status=ctx.initial_status,
        )

    def _finalize(
        self,
        ctx: _RunContext,
        *,
        verdict: TestVerdict,
        reason: str,
        last_status: HelmReleaseStatus | None,
        helm_result: CommandResult | None = None,
        in_flight: tuple[tuple[str, str, str], ...] = (),
        residual: tuple[str, ...] = (),
        inline_diagnostics: str | None = None,
    ) -> TestOutcome:
        rc = helm_result.returncode if helm_result is not None else None
        stdout = (
            truncate_bytes(helm_result.stdout or "", ctx.request.helm_test_stdout_max_bytes)
            if helm_result is not None
            else None
        )
        stderr = (
            truncate_bytes(helm_result.stderr or "", ctx.request.helm_test_stdout_max_bytes)
            if helm_result is not None
            else None
        )

        diagnostics: str | None = None
        test_pods: tuple[TestPodSnapshot, ...] = ()

        if verdict in _PASSED_VERDICTS:
            pass
        elif verdict == "skipped-not-ready":
            diagnostics = (
                "HelmRelease has not been Released; "
                "run `chart-manager helmrelease monitor` first."
            )
        else:
            diagnostics, test_pods = self._compose_diagnostics(
                ctx=ctx,
                verdict=verdict,
                reason=reason,
                last_status=last_status,
                helm_result=helm_result,
                in_flight=in_flight,
                residual=residual,
                inline=inline_diagnostics,
            )

        return TestOutcome(
            ref=ctx.ref,
            verdict=verdict,
            reason=reason,
            helm_test_returncode=rc,
            helm_test_stdout=stdout,
            helm_test_stderr=stderr,
            test_pods=test_pods,
            last_status=last_status,
            phase_log=tuple(ctx.phase_log),
            diagnostics=diagnostics,
            duration_seconds=self._clock() - ctx.started_mono,
        )

    def _compose_diagnostics(
        self,
        *,
        ctx: _RunContext,
        verdict: TestVerdict,
        reason: str,
        last_status: HelmReleaseStatus | None,
        helm_result: CommandResult | None,
        in_flight: tuple[tuple[str, str, str], ...],
        residual: tuple[str, ...],
        inline: str | None,
    ) -> tuple[str, tuple[TestPodSnapshot, ...]]:
        ns = ctx.ref.namespace or "(none)"
        name = ctx.ref.name or "(none)"
        parts: list[str] = [f"## {ns}/{name} - {verdict}: {reason}"]

        if last_status is not None:
            parts.append("\n### Status")
            for cond_type in ("Ready", "Released", "TestSuccess"):
                cond = last_status.condition(cond_type)
                if cond is None:
                    parts.append(f"- {cond_type}: (absent)")
                else:
                    parts.append(
                        f"- {cond_type}: {cond.status} ({cond.reason}) - {cond.message}"
                    )

        if in_flight:
            parts.append("\n### In-flight test pods")
            for pod_ns, pod_name, phase in in_flight:
                parts.append(f"- {pod_ns}/{pod_name} (phase={phase})")

        if residual:
            parts.append("\n### Residual test pods (delete failed)")
            for entry in residual:
                parts.append(f"- {entry}")

        if inline:
            parts.append("\n### Detail")
            parts.append(inline)

        test_pods: tuple[TestPodSnapshot, ...] = ()
        if reason in ("TestFailed", "TestPodConflict"):
            test_pods = self._snapshot_test_pods(ctx)
            if test_pods:
                parts.append("\n### Test pod logs")
                for pod in test_pods:
                    parts.append(f"\n#### {pod.namespace}/{pod.name} (phase={pod.phase})")
                    if pod.logs:
                        parts.append(pod.logs)
                    if pod.previous_logs:
                        parts.append("\n##### previous")
                        parts.append(pod.previous_logs)

        if ctx.ref.target_namespace:
            parts.append(f"\n### Events (namespace {ctx.ref.target_namespace})")
            parts.append(self._safe_events(ctx))

        if helm_result is not None and (helm_result.stdout or helm_result.stderr):
            parts.append("\n### helm test output")
            if helm_result.stdout:
                parts.append(
                    truncate_bytes(helm_result.stdout, ctx.request.helm_test_stdout_max_bytes)
                )
            if helm_result.stderr:
                parts.append("\n#### stderr")
                parts.append(
                    truncate_bytes(helm_result.stderr, ctx.request.helm_test_stdout_max_bytes)
                )

        return "\n".join(parts), test_pods

    def _snapshot_test_pods(self, ctx: _RunContext) -> tuple[TestPodSnapshot, ...]:
        try:
            pods = self._flux.list_test_pods(ctx.ref, timeout=ctx.parsed.per_poll_sec)
        except ExternalCommandError:
            return ()
        snapshots: list[TestPodSnapshot] = []
        for pod_ns, pod_name, phase in pods[: ctx.request.diagnostics_pod_cap]:
            try:
                logs = self._flux.pod_logs(
                    pod_ns,
                    pod_name,
                    tail=ctx.request.pod_log_tail,
                    previous=False,
                    timeout=ctx.parsed.per_poll_sec,
                )
            except ExternalCommandError:
                logs = ""
            previous: str | None = None
            # Only retry with --previous for terminal-phase pods where the
            # current container is gone; for Running/Pending the empty
            # response just means "no logs yet", not a restarted container.
            if not logs and phase in _STALE_PHASES:
                try:
                    previous = self._flux.pod_logs(
                        pod_ns,
                        pod_name,
                        tail=ctx.request.pod_log_tail,
                        previous=True,
                        timeout=ctx.parsed.per_poll_sec,
                    )
                except ExternalCommandError:
                    previous = None
            snapshots.append(
                TestPodSnapshot(
                    namespace=pod_ns,
                    name=pod_name,
                    phase=phase,
                    logs=truncate_bytes(logs, ctx.request.pod_log_max_bytes),
                    previous_logs=(
                        truncate_bytes(previous, ctx.request.pod_log_max_bytes)
                        if previous
                        else None
                    ),
                )
            )
        return tuple(snapshots)

    def _safe_events(self, ctx: _RunContext) -> str:
        try:
            blob = self._flux.namespace_events(
                ctx.ref.target_namespace, timeout=ctx.parsed.per_poll_sec
            )
        except ExternalCommandError as exc:
            return f"<events unavailable: {exc}>"
        return truncate_lines(blob, _EVENTS_LINE_CAP)

    # --- progress ---------------------------------------------------------

    def _fire(self, ctx: _RunContext, phase: str, detail: str) -> None:
        t = Transition(at=self._now(), phase=phase, detail=detail)
        ctx.phase_log.append(t)
        if len(ctx.phase_log) > _PHASE_LOG_MAX:
            del ctx.phase_log[0 : len(ctx.phase_log) - _PHASE_LOG_MAX]
        if self._progress is None:
            return
        try:
            self._progress(ctx.ref, t)
        except Exception:
            _LOG.exception("test progress callback raised")
