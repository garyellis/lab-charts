"""`promote test`: run `helm test` for matched Flux HelmReleases and aggregate the verdict.

Read-mostly: deletes only stale test pods, never HelmRelease specs. Each `helm test`
creates test pods, so tune `concurrency` down on small clusters.
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

import chart_manager.commands.promote.report as report
from chart_manager.commands.promote.fanout import RunResult, run_matched
from chart_manager.commands.promote.matching import filter_matched_statuses
from chart_manager.commands.promote.state import (
    NO_MATCH_REF,
    Reason,
    ReasonLike,
    Stage,
    Transition,
    Verdict,
)
from chart_manager.commands.promote.telemetry import PromotionTelemetry
from chart_manager.integrations.helm import Helm, format_helm_duration
from chart_manager.integrations.helmrelease import (
    HelmReleaseClient,
    HelmReleaseRef,
    HelmReleaseStatus,
)
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.commands import CommandResult, CommandRunner
from chart_manager.plumbing.duration import require_positive_seconds
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.plumbing.text import truncate_bytes
from chart_manager.shared.charts import dependencies
from chart_manager.shared.events.writer import EventWriter
from chart_manager.shared.settings import Settings

_LOG = logging.getLogger(__name__)

# Pod phases that mean a previous helm-test run is still live; we MUST NOT
# run helm again (helm would recreate-conflict or, worse, kill the live
# pod). Empty phase means the kubelet hasn't reported yet -- treat the
# same as Pending so we don't race the apiserver.
_IN_FLIGHT_PHASES = frozenset({"Pending", "Running", "Unknown", ""})
_STALE_PHASES = frozenset({"Succeeded", "Failed"})

_PHASE_LOG_MAX = 5
#: Allowance the `helm test` subprocess gets beyond `per_hr_timeout_seconds` (helm's
#: own `--timeout`), so helm's timeout fires before we kill the subprocess;
#: still capped by the remaining total budget.
_SUBPROCESS_SLACK_SEC = 30.0
#: Retained bytes of each current/previous test-pod log stream.
_POD_LOG_MAX_BYTES = 16_384
#: Test pods whose logs are snapshotted per HelmRelease on failure.
_DIAGNOSTICS_POD_CAP = 5
#: Retained bytes of each captured `helm test` stream, stdout and stderr alike.
_HELM_OUTPUT_MAX_BYTES = 32_768

_NO_TESTS_PATTERN = re.compile(r"no tests (to run|for chart|found)", re.IGNORECASE)
_HELM_UNAVAILABLE_PATTERN = re.compile(
    r"cluster unreachable|connection refused|INSTALLATION FAILED",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TestRequest:
    """Operator inputs for a `helm test` run over matching HelmReleases."""

    chart_name: str
    version: str
    namespace: str | None = None
    concurrency: int = 4
    # Budgets in seconds. The CLI parses its duration strings once, at the
    # input boundary; every caller gets the same numeric validation below.
    per_poll_timeout_seconds: float = 10.0
    # per_hr_timeout_seconds: per-pod readiness wait passed to helm `--timeout`.
    # Charts with multiple test hooks may exceed this wall-clock; the
    # subprocess cap (per_hr + _SUBPROCESS_SLACK_SEC, bounded by total) is the
    # hard stop.
    per_hr_timeout_seconds: float = 300.0
    total_timeout_seconds: float = 900.0
    pod_log_tail: int = 200
    # concurrency: each helm test creates 1+ test pods. concurrency=4
    # against 4 HRs with multi-pod suites may create 8-16 pods concurrently
    # on the cluster; tune down on small clusters.
    # Which promotion target these tests verify. None (the default, and what
    # an ad-hoc `promote test` passes) means the run emits no lifecycle
    # events at all -- see commands/promote/telemetry.py.
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
        require_positive_seconds("per_poll_timeout_seconds", self.per_poll_timeout_seconds)
        require_positive_seconds("per_hr_timeout_seconds", self.per_hr_timeout_seconds)
        require_positive_seconds("total_timeout_seconds", self.total_timeout_seconds)
        if self.per_hr_timeout_seconds < 30.0:
            raise ChartManagerError(
                f"per_hr_timeout_seconds ({self.per_hr_timeout_seconds:g}s) must be >= 30s"
            )
        if self.total_timeout_seconds < self.per_hr_timeout_seconds:
            raise ChartManagerError(
                f"total_timeout_seconds ({self.total_timeout_seconds:g}s) must be >= "
                f"per_hr_timeout_seconds ({self.per_hr_timeout_seconds:g}s)"
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


#: Aggregate result across all tested HelmReleases. A plain assignment, not a
#: `type` statement, so `TestResult(...)` stays callable.
TestResult = RunResult[TestOutcome]


def _no_match_outcome(elapsed: float) -> TestOutcome:
    """The single synthetic outcome of a run whose selector matched nothing."""
    return TestOutcome(
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
    )


# Internal aggregate for a single watcher; lets us thread state through
# the phase methods without dragging 8 positional args.
@dataclass
class _RunContext:
    """Mutable per-HelmRelease state threaded through the test pipeline methods."""

    ref: HelmReleaseRef
    initial_status: HelmReleaseStatus
    request: TestRequest
    started_mono: float
    total_deadline: float
    cancel_event: threading.Event
    # `deque(maxlen=)` rather than a hand-rolled slice-off: monitor's
    # ring already worked this way, and two implementations of "keep the last
    # N transitions" is one more than the concept needs.
    phase_log: deque[Transition] = field(
        default_factory=lambda: deque(maxlen=_PHASE_LOG_MAX)
    )


def run(
    request: TestRequest,
    *,
    runner: CommandRunner,
    settings: Settings,
    events: EventWriter,
    progress: Callable[[HelmReleaseRef, Transition], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> TestResult:
    """Test every matching HelmRelease in parallel; return an aggregate TestResult.

    Yields a single `no-match` outcome when nothing matches. A worker
    raising ExternalCommandError/ChartManagerError cancels the rest and
    propagates; other crashes are wrapped as ChartManagerError. `progress`
    hears each phase; the time sources are for tests.
    """
    # One kubectl serves the HelmRelease queries and the pod/event calls, so
    # both address the same cluster.
    kubectl = Kubectl(runner, context=settings.kube_context, timeout=settings.command_timeout)
    client = HelmReleaseClient(kubectl)
    # verbose=False: concurrent `helm test` streams would interleave; the
    # output is captured onto each outcome instead.
    helm = Helm(
        runner,
        verbose=False,
        context=settings.kube_context,
        deps_are_fresh=dependencies.deps_are_fresh,
        chart_has_dependencies=dependencies.chart_has_dependencies,
    )
    tester = _Tester(client, kubectl, helm, clock, progress)
    start = clock()
    matched = filter_matched_statuses(
        client,
        namespace=request.namespace,
        chart_name=request.chart_name,
        version=request.version,
        per_poll=request.per_poll_timeout_seconds,
    )

    _LOG.info(
        "helm test run started: chart=%s version=%s namespace=%s environment=%s "
        "matched=%d concurrency=%d per_hr=%gs total=%gs per_poll=%gs",
        request.chart_name,
        request.version,
        request.namespace or "(all)",
        request.environment or "(none)",
        len(matched),
        request.concurrency,
        request.per_hr_timeout_seconds,
        request.total_timeout_seconds,
        request.per_poll_timeout_seconds,
    )

    # Built unconditionally, but inert until `run_matched` finds a match:
    # a run with nothing to test opens no HELM_TEST_RUN interval. Event
    # writes are non-fatal: the tests have already run.
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
        stage=Stage.HELM_TEST,
        success=Verdict.PASSED,
        no_match=_no_match_outcome,
        work=lambda status, cancel_event: tester.test_one(
            status, request, total_deadline, cancel_event
        ),
        crash_label="test watcher",
        # No `cancel_on`: unlike monitor there is no --fail-fast here.
        # A failing chart's tests say nothing about its peers', and the
        # operator wants the whole matrix, not the first red cell.
        log_label="helm test",
        chart_name=request.chart_name,
        version=request.version,
        namespace=request.namespace,
    )


@dataclass(frozen=True)
class _Tester:
    """Runs the per-HelmRelease pipeline; one instance serves every worker thread."""

    client: HelmReleaseClient
    kubectl: Kubectl
    helm: Helm
    clock: Callable[[], float]
    progress: Callable[[HelmReleaseRef, Transition], None] | None

    # --- per-HR pipeline ---------------------------------------------------

    def test_one(
        self,
        initial_status: HelmReleaseStatus,
        request: TestRequest,
        total_deadline: float,
        cancel_event: threading.Event,
    ) -> TestOutcome:
        """Run the full pipeline for one HelmRelease: preflight -> reap -> helm test."""
        ctx = _RunContext(
            ref=initial_status.ref,
            initial_status=initial_status,
            request=request,
            started_mono=self.clock(),
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

        if ctx.cancel_event.is_set() or self.clock() >= ctx.total_deadline:
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
        if ctx.cancel_event.is_set() or self.clock() >= ctx.total_deadline:
            return self._finalize_timed_out(ctx, Reason.TOTAL_BUDGET_EXHAUSTED)
        return None

    def _reap(self, ctx: _RunContext) -> TestOutcome | None:
        """Clear leftover test pods; fail if any are in-flight or won't delete.

        Returns None when the caller should proceed.
        """
        self._fire(ctx, "Reaping", "checking for existing test pods")
        try:
            pods = self.client.list_test_pods(
                ctx.ref, timeout=ctx.request.per_poll_timeout_seconds
            )
        except ExternalCommandError as exc:
            _LOG.error(
                "test pod listing failed during reap: ns=%s name=%s: %s",
                ctx.ref.namespace,
                ctx.ref.name,
                report.failure_detail(exc),
            )
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
                self.kubectl.delete_pod(ns, name, timeout=ctx.request.per_poll_timeout_seconds)
            except ExternalCommandError as exc:
                # Carry the stderr, not just the pod name: "delete denied by
                # RBAC", "apiserver unreachable" and "stuck on a finalizer"
                # are three different operator actions and rendered as one.
                # Logged as well as rendered: the report reaches the caller,
                # the log reaches whoever reads CI afterwards.
                _LOG.warning(
                    "stale test pod delete failed: ns=%s pod=%s release=%s: %s",
                    ns,
                    name,
                    ctx.ref.name,
                    report.failure_detail(exc),
                )
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
        remaining_total = max(0.0, ctx.total_deadline - self.clock())
        subprocess_cap = min(
            ctx.request.per_hr_timeout_seconds + _SUBPROCESS_SLACK_SEC, remaining_total
        )
        if subprocess_cap <= 0:
            return self._finalize_timed_out(ctx, Reason.TOTAL_BUDGET_EXHAUSTED)

        try:
            result = self.helm.test(
                ctx.ref.release_name,
                namespace=ctx.ref.storage_namespace,
                # helm's per-hook `--timeout` stays the uncapped per-HR budget;
                # `subprocess_cap` above is the hard wall-clock stop.
                timeout=format_helm_duration(ctx.request.per_hr_timeout_seconds),
                logs=True,
                subprocess_timeout=subprocess_cap,
            )
        except ExternalCommandError as exc:
            msg = str(exc)
            if "timed out" in msg:
                reason = (
                    Reason.TOTAL_BUDGET_EXHAUSTED
                    if self.clock() >= ctx.total_deadline
                    else Reason.PER_HR_BUDGET_EXHAUSTED
                )
                # Which budget tripped decides whether the operator raises
                # --per-hr-timeout or accepts the run was too big.
                _LOG.warning(
                    "helm test timed out: ns=%s name=%s reason=%s per_hr=%gs total=%gs",
                    ctx.ref.namespace,
                    ctx.ref.name,
                    reason,
                    ctx.request.per_hr_timeout_seconds,
                    ctx.request.total_timeout_seconds,
                )
                return self._finalize_timed_out(ctx, reason)
            _LOG.error(
                "helm test invocation failed outside a verdict: ns=%s name=%s: %s",
                ctx.ref.namespace,
                ctx.ref.name,
                msg,
            )
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
            truncate_bytes(helm_result.stdout or "", _HELM_OUTPUT_MAX_BYTES)
            if helm_result is not None
            else None
        )
        stderr = (
            truncate_bytes(helm_result.stderr or "", _HELM_OUTPUT_MAX_BYTES)
            if helm_result is not None
            else None
        )

        # Measured before diagnostics: composing them lists test pods and
        # scrapes up to two log streams per pod plus namespace events, all on
        # the failure path. Folding that into the reported duration inflates
        # exactly the outcomes whose timing matters most.
        duration_seconds = self.clock() - ctx.started_mono

        diagnostics: str | None = None
        test_pods: tuple[TestPodSnapshot, ...] = ()

        if verdict.is_passing:
            pass
        elif verdict is Verdict.SKIPPED_NOT_READY:
            diagnostics = (
                "HelmRelease has not been Released; "
                "run `chart-manager promote monitor` first."
            )
        else:
            # One line per non-passing release, carrying the pair (verdict,
            # reason) the rendered report leads with. The report itself stays
            # out of the log: it is multi-kilobyte markdown and the caller
            # already holds it.
            _LOG.warning(
                "helm test outcome failed: ns=%s name=%s verdict=%s reason=%s "
                "rc=%s elapsed=%.1fs",
                ctx.ref.namespace,
                ctx.ref.name,
                verdict,
                reason,
                rc,
                duration_seconds,
            )
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
            status = self.client.get_status(
                ctx.ref, timeout=ctx.request.per_poll_timeout_seconds
            )
            return status, None
        except ExternalCommandError as exc:
            detail = report.failure_detail(exc)
            _LOG.warning(
                "HelmRelease status refresh failed; report keeps the pre-run status: "
                "ns=%s name=%s: %s",
                ctx.ref.namespace,
                ctx.ref.name,
                detail,
            )
            return None, detail

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
                        self.kubectl.namespace_events,
                        ctx.ref.target_namespace,
                        timeout=ctx.request.per_poll_timeout_seconds,
                    )
                )
            )

        if helm_result is not None and (helm_result.stdout or helm_result.stderr):
            parts.append("\n### helm test output")
            if helm_result.stdout:
                parts.append(
                    truncate_bytes(helm_result.stdout, _HELM_OUTPUT_MAX_BYTES)
                )
            if helm_result.stderr:
                parts.append("\n#### stderr")
                parts.append(
                    truncate_bytes(helm_result.stderr, _HELM_OUTPUT_MAX_BYTES)
                )

        return "\n".join(parts), test_pods

    def _snapshot_test_pods(
        self, ctx: _RunContext
    ) -> tuple[tuple[TestPodSnapshot, ...], str | None]:
        """Collect logs for up to `_DIAGNOSTICS_POD_CAP` test pods; falls back to --previous logs.

        Returns `(snapshots, None)`, or `((), detail)` when the pods could not
        be listed at all -- which the caller must render differently from an
        empty list.
        """
        try:
            pods = self.client.list_test_pods(
                ctx.ref, timeout=ctx.request.per_poll_timeout_seconds
            )
        except ExternalCommandError as exc:
            detail = report.failure_detail(exc)
            # "we never got to look" vs "the chart left nothing behind": the
            # report distinguishes them and so must the log.
            _LOG.warning(
                "test pod listing failed while composing diagnostics: ns=%s name=%s: %s",
                ctx.ref.namespace,
                ctx.ref.name,
                detail,
            )
            return (), detail
        snapshots: list[TestPodSnapshot] = []
        for pod_ns, pod_name, phase in pods[:_DIAGNOSTICS_POD_CAP]:
            log_error: str | None = None
            logs = ""
            try:
                logs = self.kubectl.pod_logs(
                    pod_ns,
                    pod_name,
                    tail=ctx.request.pod_log_tail,
                    previous=False,
                    timeout=ctx.request.per_poll_timeout_seconds,
                )
            except ExternalCommandError as exc:
                log_error = report.failure_detail(exc)
                _LOG.warning(
                    "test pod logs unavailable: ns=%s pod=%s phase=%s release=%s: %s",
                    pod_ns,
                    pod_name,
                    phase,
                    ctx.ref.name,
                    log_error,
                )
            previous: str | None = None
            # Only retry with --previous for terminal-phase pods where the
            # current container is gone; for Running/Pending the empty
            # response just means "no logs yet", not a restarted container.
            # A failed fetch is neither, and retrying it just spends another
            # round trip to record the same failure twice.
            if log_error is None and not logs and phase in _STALE_PHASES:
                _LOG.debug(
                    "retrying test pod logs with --previous: ns=%s pod=%s phase=%s",
                    pod_ns,
                    pod_name,
                    phase,
                )
                try:
                    previous = self.kubectl.pod_logs(
                        pod_ns,
                        pod_name,
                        tail=ctx.request.pod_log_tail,
                        previous=True,
                        timeout=ctx.request.per_poll_timeout_seconds,
                    )
                except ExternalCommandError as exc:
                    # The only capture site with nowhere to render: a failed
                    # --previous fetch produces `previous_logs=None`, which is
                    # exactly what "the container never restarted" produces.
                    _LOG.warning(
                        "previous test pod logs unavailable: ns=%s pod=%s: %s",
                        pod_ns,
                        pod_name,
                        report.failure_detail(exc),
                    )
                    previous = None
            snapshots.append(
                TestPodSnapshot(
                    namespace=pod_ns,
                    name=pod_name,
                    phase=phase,
                    logs=(
                        f"<logs unavailable: {log_error}>"
                        if log_error is not None
                        else truncate_bytes(logs, _POD_LOG_MAX_BYTES)
                    ),
                    previous_logs=(
                        truncate_bytes(previous, _POD_LOG_MAX_BYTES)
                        if previous
                        else None
                    ),
                )
            )
        return tuple(snapshots), None

    # --- progress ---------------------------------------------------------

    def _fire(self, ctx: _RunContext, phase: str, detail: str) -> None:
        """Record a phase transition (ring-buffered) and fire the progress callback safely."""
        t = Transition(at=datetime.now(UTC), phase=phase, detail=detail)
        ctx.phase_log.append(t)
        if self.progress is None:
            return
        try:
            self.progress(ctx.ref, t)
        except Exception:
            _LOG.exception("test progress callback raised")
