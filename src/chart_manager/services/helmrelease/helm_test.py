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
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.helmrelease import (
    HelmReleaseClient,
    HelmReleaseRef,
    HelmReleaseStatus,
)
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.commands import CommandResult
from chart_manager.plumbing.duration import parse_duration
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.plumbing.text import truncate_bytes
from chart_manager.services.events.writer import EventWriter
from chart_manager.services.helmrelease import report
from chart_manager.services.helmrelease.fanout import run_fanout, sorted_by_ref
from chart_manager.services.helmrelease.matching import filter_matched_statuses
from chart_manager.services.helmrelease.state import (
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

# Pod phases that mean a previous helm-test run is still live; we MUST NOT
# run helm again (helm would recreate-conflict or, worse, kill the live
# pod). Empty phase means the kubelet hasn't reported yet -- treat the
# same as Pending so we don't race the apiserver.
_IN_FLIGHT_PHASES = frozenset({"Pending", "Running", "Unknown", ""})
_STALE_PHASES = frozenset({"Succeeded", "Failed"})

_PHASE_LOG_MAX = 5

_NO_TESTS_PATTERN = re.compile(r"no tests (to run|for chart|found)", re.IGNORECASE)
_HELM_UNAVAILABLE_PATTERN = re.compile(
    r"cluster unreachable|connection refused|INSTALLATION FAILED",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TestRequest:
    """Inputs and tunables for a `helm test` run over matching HelmReleases."""

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
    # Which promotion target these tests verify. None (the default, and what
    # an ad-hoc `helmrelease test` passes) means the run emits no lifecycle
    # events at all -- see services/helmrelease/telemetry.py.
    environment: str | None = None

    def __post_init__(self) -> None:
        """Validate the tunables; raise ChartManagerError on any out-of-range value."""
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
    """Captured logs + phase for one test pod, gathered for failure diagnostics."""

    namespace: str
    name: str
    phase: str
    logs: str
    previous_logs: str | None


@dataclass(frozen=True)
class TestOutcome:
    """Result of testing one HelmRelease: verdict, helm output, pods, and diagnostics."""

    ref: HelmReleaseRef
    verdict: Verdict
    reason: ReasonLike
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
    """Aggregate result across all tested HelmReleases."""

    outcomes: tuple[TestOutcome, ...]
    total_duration_seconds: float
    total_timed_out: bool

    @property
    def ok(self) -> bool:
        """True only if there were outcomes and every one passed."""
        return bool(self.outcomes) and all(
            o.verdict in PASSING_VERDICTS for o in self.outcomes
        )

    @property
    def failures(self) -> tuple[TestOutcome, ...]:
        """Outcomes whose verdict is not a passing one."""
        return tuple(o for o in self.outcomes if o.verdict not in PASSING_VERDICTS)


@dataclass
class _ParsedRequest:
    """The request's duration strings parsed once into seconds."""

    per_poll_sec: float
    per_hr_sec: float
    total_sec: float
    subprocess_slack_sec: float


# Internal aggregate for a single watcher; lets us thread state through
# the phase methods without dragging 8 positional args.
@dataclass
class _RunContext:
    """Mutable per-HelmRelease state threaded through the test pipeline methods."""

    ref: HelmReleaseRef
    initial_status: HelmReleaseStatus
    parsed: _ParsedRequest
    request: TestRequest
    started_mono: float
    total_deadline: float
    cancel_event: threading.Event
    # `deque(maxlen=)` rather than a hand-rolled slice-off: MonitorService's
    # ring already worked this way, and two implementations of "keep the last
    # N transitions" is one more than the concept needs.
    phase_log: deque[Transition] = field(
        default_factory=lambda: deque(maxlen=_PHASE_LOG_MAX)
    )


class TestService:
    """Run `helm test` across matching HelmReleases concurrently, with reaping + diagnostics."""

    def __init__(
        self,
        client: HelmReleaseClient | None = None,
        helm: Helm | None = None,
        *,
        kubectl: Kubectl | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        progress: Callable[[HelmReleaseRef, Transition], None] | None = None,
        events: EventWriter | None = None,
        strict_events: bool = False,
    ) -> None:
        """Wire the HelmRelease/kubectl/helm clients and injectable clock/now/progress hooks."""
        self._client = client or HelmReleaseClient()
        # Two cluster adapters, not one: HelmRelease queries are Flux domain
        # knowledge; reaping test pods, scraping their logs and dumping
        # namespace events are plain kubectl. Both must address the same
        # cluster -- `Container` builds them from one `Settings.kube_context`.
        self._kubectl = kubectl or Kubectl()
        # verbose=False prevents 4 concurrent helm test stdout streams from
        # interleaving into garbage; the service captures and returns
        # stdout/stderr on the result instead.
        self._helm = helm or Helm(verbose=False)
        self._clock = clock
        self._now = now
        self._progress = progress
        # Same non-fatal policy as PromoteService and MonitorService: the
        # tests have already run by the time these are written, so an
        # unconfigured events backend must not turn a green suite into a
        # traceback. `strict` is for callers where the event is the deliverable.
        self._events = events or EventWriter()
        self._strict_events = strict_events

    def test(self, request: TestRequest) -> TestResult:
        """Test every matching HelmRelease in parallel; return an aggregate TestResult.

        Yields a single `no-match` outcome when nothing matches. A worker
        raising ExternalCommandError/ChartManagerError cancels the rest and
        propagates; other crashes are wrapped as ChartManagerError.
        """
        start = self._clock()
        parsed = _ParsedRequest(
            per_poll_sec=parse_duration(request.per_poll_timeout),
            per_hr_sec=parse_duration(request.per_hr_timeout),
            total_sec=parse_duration(request.total_timeout),
            subprocess_slack_sec=parse_duration(request.subprocess_slack),
        )

        matched = filter_matched_statuses(
            self._client,
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
                        verdict=Verdict.NO_MATCH,
                        reason=Reason.NO_HELMRELEASES_MATCHED,
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

        # Emitted after the zero-match return: a run with nothing to test
        # opened no interval, so bracketing it would put a HELM_TEST_RUN on
        # the timeline that nothing will ever close.
        telemetry = PromotionTelemetry(
            writer=self._events,
            chart_name=request.chart_name,
            version=request.version,
            environment=request.environment,
            strict=self._strict_events,
        )
        telemetry.started(Stage.HELM_TEST, matched=len(matched))

        total_deadline = start + parsed.total_sec
        cancel_event = threading.Event()
        outcomes: list[TestOutcome] = []

        try:
            run_fanout(
                matched,
                concurrency=request.concurrency,
                clock=self._clock,
                total_deadline=total_deadline,
                cancel_event=cancel_event,
                outcomes=outcomes,
                work=lambda status: self._test_one(
                    status, parsed, request, total_deadline, cancel_event
                ),
                crash_label="test watcher",
                # No `cancel_on`: unlike monitor there is no --fail-fast here.
                # A failing chart's tests say nothing about its peers', and the
                # operator wants the whole matrix, not the first red cell.
            )
        except Exception:
            # An infrastructure failure still ends the interval opened above.
            # Without this the timeline keeps a HELM_TEST_RUN that nothing
            # ever closes -- the exact defect this wiring exists to remove.
            #
            # `Exception`, not `BaseException`: Ctrl-C must kill a long
            # parallel run immediately, and this handler would put a network
            # write in front of the exit. An interrupted run genuinely has no
            # terminal state to report.
            telemetry.finished(
                Stage.HELM_TEST,
                Verdict.FAILED,
                total=len(matched),
                failures=len(matched) - len(outcomes),
            )
            raise

        elapsed = self._clock() - start
        result = TestResult(
            outcomes=sorted_by_ref(outcomes),
            total_duration_seconds=elapsed,
            total_timed_out=cancel_event.is_set(),
        )
        telemetry.finished(
            Stage.HELM_TEST,
            run_verdict((o.verdict for o in result.outcomes), success=Verdict.PASSED),
            total=len(result.outcomes),
            failures=len(result.failures),
        )
        return result

    # --- per-HR pipeline ---------------------------------------------------

    def _test_one(
        self,
        initial_status: HelmReleaseStatus,
        parsed: _ParsedRequest,
        request: TestRequest,
        total_deadline: float,
        cancel_event: threading.Event,
    ) -> TestOutcome:
        """Run the full pipeline for one HelmRelease: preflight -> reap -> helm test."""
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
            return self._finalize_timed_out(ctx, Reason.TOTAL_BUDGET_EXHAUSTED)

        return self._run_helm(ctx)

    def _preflight(self, ctx: _RunContext) -> TestOutcome | None:
        """Skip if suspended/not-released/generation-lagging; None means proceed."""
        s = ctx.initial_status
        if s.suspended:
            return self._finalize(
                ctx,
                verdict=Verdict.SKIPPED_SUSPENDED,
                reason=Reason.SUSPENDED,
                last_status=s,
            )
        released = s.released
        if released is None or released.status != "True":
            return self._finalize(
                ctx,
                verdict=Verdict.SKIPPED_NOT_READY,
                reason=Reason.NOT_RELEASED,
                last_status=s,
            )
        if s.observed_generation != s.generation:
            return self._finalize(
                ctx,
                verdict=Verdict.SKIPPED_NOT_READY,
                reason=Reason.GENERATION_LAG,
                last_status=s,
            )
        if ctx.cancel_event.is_set() or self._clock() >= ctx.total_deadline:
            return self._finalize_timed_out(ctx, Reason.TOTAL_BUDGET_EXHAUSTED)
        return None

    def _reap(self, ctx: _RunContext) -> TestOutcome | None:
        """Clear leftover test pods; fail if any are in-flight or won't delete.

        Returns None when the caller should proceed.
        """
        self._fire(ctx, "Reaping", "checking for existing test pods")
        try:
            pods = self._client.list_test_pods(ctx.ref, timeout=ctx.parsed.per_poll_sec)
        except ExternalCommandError as exc:
            return self._finalize(
                ctx,
                verdict=Verdict.FAILED,
                reason=Reason.REAP_LIST_FAILED,
                last_status=ctx.initial_status,
                inline_diagnostics=str(exc),
            )

        in_flight = [p for p in pods if p[2] in _IN_FLIGHT_PHASES]
        if in_flight:
            return self._finalize(
                ctx,
                verdict=Verdict.FAILED,
                reason=Reason.TEST_POD_IN_FLIGHT,
                last_status=ctx.initial_status,
                in_flight=tuple(in_flight),
            )

        residual: list[str] = []
        for ns, name, _phase in [p for p in pods if p[2] in _STALE_PHASES]:
            try:
                self._kubectl.delete_pod(ns, name, timeout=ctx.parsed.per_poll_sec)
            except ExternalCommandError as exc:
                # Carry the stderr, not just the pod name: "delete denied by
                # RBAC", "apiserver unreachable" and "stuck on a finalizer"
                # are three different operator actions and rendered as one.
                residual.append(f"{ns}/{name}: {report.failure_detail(exc)}")
        if residual:
            return self._finalize(
                ctx,
                verdict=Verdict.FAILED,
                reason=Reason.REAP_INCOMPLETE,
                last_status=ctx.initial_status,
                residual=tuple(residual),
            )
        return None

    def _run_helm(self, ctx: _RunContext) -> TestOutcome:
        """Invoke `helm test` (subprocess cap bounded by the total deadline) and classify."""
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
            return self._finalize_timed_out(ctx, Reason.TOTAL_BUDGET_EXHAUSTED)

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
                    Reason.TOTAL_BUDGET_EXHAUSTED
                    if self._clock() >= ctx.total_deadline
                    else Reason.PER_HR_BUDGET_EXHAUSTED
                )
                return self._finalize_timed_out(ctx, reason)
            # Defensive: with check=False the runner shouldn't raise on
            # rc != 0, but propagate any other surprise as HelmUnavailable
            # so we still produce a structured outcome.
            return self._finalize(
                ctx,
                verdict=Verdict.FAILED,
                reason=Reason.HELM_UNAVAILABLE,
                last_status=ctx.initial_status,
                helm_result=None,
                inline_diagnostics=msg,
            )

        return self._classify(ctx, result)

    def _classify(self, ctx: _RunContext, result: CommandResult) -> TestOutcome:
        """Map helm's rc/stderr to a verdict (rc 0 passes; 'no tests' also passes)."""
        stderr = result.stderr or ""
        rc = result.returncode

        if rc == 0:
            self._fire(ctx, "Finished", "passed")
            return self._finalize(
                ctx,
                verdict=Verdict.PASSED,
                reason=Reason.ALL_TESTS_PASSED,
                last_status=ctx.initial_status,
                helm_result=result,
            )

        # Charts with no `helm.sh/hook=test` templates report rc != 0 with
        # a stderr line matching one of these phrasings. Treat as passed,
        # no diagnostics, no cluster event calls.
        if _NO_TESTS_PATTERN.search(stderr):
            self._fire(ctx, "Finished", "no tests defined")
            return self._finalize(
                ctx,
                verdict=Verdict.PASSED,
                reason=Reason.NO_TESTS_DEFINED,
                last_status=ctx.initial_status,
                helm_result=result,
            )

        if _HELM_UNAVAILABLE_PATTERN.search(stderr):
            self._fire(ctx, "Finished", "helm unavailable")
            return self._finalize(
                ctx,
                verdict=Verdict.FAILED,
                reason=Reason.HELM_UNAVAILABLE,
                last_status=ctx.initial_status,
                helm_result=result,
            )

        if "already exists" in stderr.lower():
            self._fire(ctx, "Finished", "test pod conflict")
            return self._finalize(
                ctx,
                verdict=Verdict.FAILED,
                reason=Reason.TEST_POD_CONFLICT,
                last_status=ctx.initial_status,
                helm_result=result,
            )

        self._fire(ctx, "Finished", f"failed (rc={rc})")
        return self._finalize(
            ctx,
            verdict=Verdict.FAILED,
            reason=Reason.TEST_FAILED,
            last_status=ctx.initial_status,
            helm_result=result,
        )

    # --- finalize / diagnostics -------------------------------------------

    def _finalize_timed_out(self, ctx: _RunContext, reason: Reason) -> TestOutcome:
        """Finalize with the `timed-out` verdict for a budget-exhaustion reason."""
        return self._finalize(
            ctx,
            verdict=Verdict.TIMED_OUT,
            reason=reason,
            last_status=ctx.initial_status,
        )

    def _finalize(
        self,
        ctx: _RunContext,
        *,
        verdict: Verdict,
        reason: ReasonLike,
        last_status: HelmReleaseStatus | None,
        helm_result: CommandResult | None = None,
        in_flight: tuple[tuple[str, str, str], ...] = (),
        residual: tuple[str, ...] = (),
        inline_diagnostics: str | None = None,
    ) -> TestOutcome:
        """Assemble the TestOutcome, composing diagnostics for non-passing verdicts."""
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

        # Measured before diagnostics: composing them lists test pods and
        # scrapes up to two log streams per pod plus namespace events, all on
        # the failure path. Folding that into the reported duration inflates
        # exactly the outcomes whose timing matters most.
        duration_seconds = self._clock() - ctx.started_mono

        diagnostics: str | None = None
        test_pods: tuple[TestPodSnapshot, ...] = ()

        if verdict.is_passing:
            pass
        elif verdict is Verdict.SKIPPED_NOT_READY:
            diagnostics = (
                "HelmRelease has not been Released; "
                "run `chart-manager helmrelease monitor` first."
            )
        else:
            # Every caller hands us ctx.initial_status -- the status as it
            # was *before* `helm test` ran -- so the report's TestSuccess
            # row showed the previous reconcile's value, which is actively
            # misleading in the one artifact read after a failure. Refresh
            # once, on the failure path only, and keep the pre-run status if
            # the cluster can no longer be reached -- saying so in the report,
            # because a silent fallback reads exactly like a fresh read.
            refreshed, stale_status = self._refresh_status(ctx)
            if refreshed is not None:
                last_status = refreshed
            diagnostics, test_pods = self._compose_diagnostics(
                ctx=ctx,
                verdict=verdict,
                reason=reason,
                last_status=last_status,
                helm_result=helm_result,
                in_flight=in_flight,
                residual=residual,
                inline=inline_diagnostics,
                stale_status=stale_status,
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
            duration_seconds=duration_seconds,
        )

    def _refresh_status(
        self, ctx: _RunContext
    ) -> tuple[HelmReleaseStatus | None, str | None]:
        """Re-read the HelmRelease status for failure reporting.

        Returns `(status, None)` on success and `(None, detail)` when the read
        failed. Best-effort by design: this runs while composing a failure
        report, so a cluster that has become unreachable must not replace the
        diagnostics with an exception -- but the caller has to be able to mark
        the pre-run status it falls back to, so the detail comes back with it.
        """
        try:
            return self._client.get_status(ctx.ref, timeout=ctx.parsed.per_poll_sec), None
        except ExternalCommandError as exc:
            return None, report.failure_detail(exc)

    def _compose_diagnostics(
        self,
        *,
        ctx: _RunContext,
        verdict: Verdict,
        reason: ReasonLike,
        last_status: HelmReleaseStatus | None,
        helm_result: CommandResult | None,
        in_flight: tuple[tuple[str, str, str], ...],
        residual: tuple[str, ...],
        inline: str | None,
        stale_status: str | None,
    ) -> tuple[str, tuple[TestPodSnapshot, ...]]:
        """Render a markdown failure report and (for test failures) snapshot pod logs."""
        parts: list[str] = [report.header(ctx.ref, verdict, reason)]

        if last_status is not None:
            # No "Stalled" row, unlike monitor's report: nothing here branches
            # on it. A test verdict comes from helm's exit code, and Stalled
            # describes the reconciler that ran before helm was invoked.
            parts.extend(report.conditions(last_status, ("Ready", "Released", "TestSuccess")))
        if stale_status is not None:
            parts.append(
                f"- (status not refreshed: {stale_status}; any rows above predate `helm test`)"
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
        if reason in (Reason.TEST_FAILED, Reason.TEST_POD_CONFLICT):
            test_pods, pods_unavailable = self._snapshot_test_pods(ctx)
            if pods_unavailable is not None:
                # Not the same statement as "no test pods": one says the chart
                # left nothing behind, the other says we never got to look.
                parts.append("\n### Test pod logs")
                parts.append(f"<test pods unavailable: {pods_unavailable}>")
            elif test_pods:
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
            parts.append(
                report.safe_events(
                    partial(
                        self._kubectl.namespace_events,
                        ctx.ref.target_namespace,
                        timeout=ctx.parsed.per_poll_sec,
                    )
                )
            )

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

    def _snapshot_test_pods(
        self, ctx: _RunContext
    ) -> tuple[tuple[TestPodSnapshot, ...], str | None]:
        """Collect logs for up to `diagnostics_pod_cap` test pods; falls back to --previous logs.

        Returns `(snapshots, None)`, or `((), detail)` when the pods could not
        be listed at all -- which the caller must render differently from an
        empty list.
        """
        try:
            pods = self._client.list_test_pods(ctx.ref, timeout=ctx.parsed.per_poll_sec)
        except ExternalCommandError as exc:
            return (), report.failure_detail(exc)
        snapshots: list[TestPodSnapshot] = []
        for pod_ns, pod_name, phase in pods[: ctx.request.diagnostics_pod_cap]:
            log_error: str | None = None
            logs = ""
            try:
                logs = self._kubectl.pod_logs(
                    pod_ns,
                    pod_name,
                    tail=ctx.request.pod_log_tail,
                    previous=False,
                    timeout=ctx.parsed.per_poll_sec,
                )
            except ExternalCommandError as exc:
                log_error = report.failure_detail(exc)
            previous: str | None = None
            # Only retry with --previous for terminal-phase pods where the
            # current container is gone; for Running/Pending the empty
            # response just means "no logs yet", not a restarted container.
            # A failed fetch is neither, and retrying it just spends another
            # round trip to record the same failure twice.
            if log_error is None and not logs and phase in _STALE_PHASES:
                try:
                    previous = self._kubectl.pod_logs(
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
                    logs=(
                        f"<logs unavailable: {log_error}>"
                        if log_error is not None
                        else truncate_bytes(logs, ctx.request.pod_log_max_bytes)
                    ),
                    previous_logs=(
                        truncate_bytes(previous, ctx.request.pod_log_max_bytes)
                        if previous
                        else None
                    ),
                )
            )
        return tuple(snapshots), None

    # --- progress ---------------------------------------------------------

    def _fire(self, ctx: _RunContext, phase: str, detail: str) -> None:
        """Record a phase transition (ring-buffered) and fire the progress callback safely."""
        t = Transition(at=self._now(), phase=phase, detail=detail)
        ctx.phase_log.append(t)
        if self._progress is None:
            return
        try:
            self._progress(ctx.ref, t)
        except Exception:
            _LOG.exception("test progress callback raised")
