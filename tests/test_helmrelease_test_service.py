"""Coverage for the helmrelease TestService.

Drives concurrent helm-test execution across matched Flux HelmReleases.
All Flux/Helm interactions are faked; sleep/clock/now are injected so the
suite is fast and deterministic.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from chart_manager.integrations.flux import (
    ConditionSnapshot,
    HelmReleaseRef,
    HelmReleaseStatus,
)
from chart_manager.plumbing.commands import CommandResult
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.helmrelease._common import Transition
from chart_manager.services.helmrelease.test import (
    TestRequest,
    TestService,
)

CHART = "loki"
VERSION = "0.2.0"
WALL_BASE = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)


def _ref(name: str = "loki", namespace: str = "loki") -> HelmReleaseRef:
    return HelmReleaseRef(
        name=name,
        namespace=namespace,
        api_version="helm.toolkit.fluxcd.io/v2",
        release_name=name,
        storage_namespace=namespace,
        target_namespace=namespace,
    )


def _cond(
    type_: str,
    status: str,
    *,
    reason: str = "",
    message: str = "",
) -> ConditionSnapshot:
    return ConditionSnapshot(
        type=type_, status=status, reason=reason, message=message, last_transition_time=None
    )


def _released_status(
    ref: HelmReleaseRef,
    *,
    generation: int = 1,
    observed_generation: int = 1,
    suspended: bool = False,
    chart_name: str | None = CHART,
    version: str | None = VERSION,
) -> HelmReleaseStatus:
    return HelmReleaseStatus(
        ref=ref,
        observed_at=WALL_BASE,
        generation=generation,
        observed_generation=observed_generation,
        resource_version="1",
        suspended=suspended,
        desired_chart_name=chart_name,
        desired_chart_version=version,
        last_applied_revision=None,
        history_chart_version=version,
        conditions=(
            _cond("Ready", "True", reason="ReconciliationSucceeded"),
            _cond("Released", "True", reason="InstallSucceeded"),
        ),
    )


def _ok_result(stdout: str = "OK") -> CommandResult:
    return CommandResult(args=(), returncode=0, stdout=stdout, stderr="")


def _bad_result(stderr: str, *, returncode: int = 1, stdout: str = "") -> CommandResult:
    return CommandResult(args=(), returncode=returncode, stdout=stdout, stderr=stderr)


@dataclass
class _FakeFlux:
    list_result: list[HelmReleaseRef] = field(default_factory=list)
    statuses: dict[tuple[str, str], HelmReleaseStatus | BaseException] = field(
        default_factory=dict
    )
    test_pods: dict[tuple[str, str], list[tuple[str, str, str]] | BaseException] = field(
        default_factory=dict
    )
    delete_failures: set[tuple[str, str]] = field(default_factory=set)
    pod_logs_map: dict[tuple[str, str, bool], str] = field(default_factory=dict)
    namespace_events_result: str = "ns event\n"
    namespace_events_exc: BaseException | None = None
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    list_test_pods_count: int = 0

    def _record(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        with self.lock:
            self.calls.append((name, args, kwargs))

    def list(
        self, *, namespace: str | None = None, timeout: float | None = None
    ) -> list[HelmReleaseRef]:
        self._record("list", (), {"namespace": namespace, "timeout": timeout})
        return list(self.list_result)

    def get_status(
        self, ref: HelmReleaseRef, *, timeout: float | None = None
    ) -> HelmReleaseStatus:
        self._record("get_status", (ref,), {"timeout": timeout})
        item = self.statuses[(ref.namespace, ref.name)]
        if isinstance(item, BaseException):
            raise item
        return item

    def list_test_pods(
        self, ref: HelmReleaseRef, *, timeout: float | None = None
    ) -> list[tuple[str, str, str]]:
        with self.lock:
            self.list_test_pods_count += 1
        self._record("list_test_pods", (ref,), {"timeout": timeout})
        item = self.test_pods.get((ref.namespace, ref.name), [])
        if isinstance(item, BaseException):
            raise item
        return list(item)

    def delete_pod(
        self, namespace: str, name: str, *, timeout: float | None = None
    ) -> None:
        self._record("delete_pod", (namespace, name), {"timeout": timeout})
        if (namespace, name) in self.delete_failures:
            raise ExternalCommandError("delete failed", stderr="boom")

    def namespace_events(self, namespace: str, *, timeout: float | None = None) -> str:
        self._record("namespace_events", (namespace,), {"timeout": timeout})
        if self.namespace_events_exc is not None:
            raise self.namespace_events_exc
        return self.namespace_events_result

    def pod_logs(
        self,
        namespace: str,
        name: str,
        *,
        container: str | None = None,
        tail: int = 200,
        previous: bool = False,
        timeout: float | None = None,
    ) -> str:
        self._record(
            "pod_logs",
            (namespace, name),
            {"tail": tail, "previous": previous, "timeout": timeout},
        )
        return self.pod_logs_map.get((namespace, name, previous), "")


@dataclass
class _FakeHelm:
    """Scripted `Helm.test`. One entry per (release, namespace) reused for re-runs.

    `result_fn` overrides `result` and can return per-call (used for the
    concurrency test to record threading.get_ident()).
    """

    result: CommandResult | BaseException = field(default_factory=_ok_result)
    result_fn: Callable[[str, str], CommandResult] | None = None
    sleep_for: float = 0.0
    sleeper: Callable[[float], None] | None = None
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    threads: list[int] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def test(
        self,
        release: str,
        *,
        namespace: str,
        timeout: str = "10m",
        logs: bool = False,
        subprocess_timeout: float | None = None,
    ) -> CommandResult:
        with self.lock:
            self.threads.append(threading.get_ident())
        self.calls.append(
            (
                (release,),
                {
                    "namespace": namespace,
                    "timeout": timeout,
                    "logs": logs,
                    "subprocess_timeout": subprocess_timeout,
                },
            )
        )
        if self.sleeper is not None and self.sleep_for > 0:
            self.sleeper(self.sleep_for)
        if self.result_fn is not None:
            return self.result_fn(release, namespace)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _Clock:
    def __init__(self, start: float = 0.0, step: float = 0.0) -> None:
        self.t = start
        self.step = step

    def __call__(self) -> float:
        v = self.t
        self.t += self.step
        return v


def _make_service(
    flux: _FakeFlux,
    helm: _FakeHelm | None = None,
    *,
    clock: Callable[[], float] | None = None,
    now: Callable[[], datetime] | None = None,
    progress: Callable[[HelmReleaseRef, Transition], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> TestService:
    return TestService(
        flux,  # type: ignore[arg-type]
        helm or _FakeHelm(),  # type: ignore[arg-type]
        sleep=sleep or (lambda _t: None),
        clock=clock or _Clock(),
        now=now or (lambda: WALL_BASE),
        progress=progress,
    )


def _req(**overrides: Any) -> TestRequest:
    base: dict[str, Any] = {
        "chart_name": CHART,
        "version": VERSION,
        "concurrency": 2,
        "per_poll_timeout": "10s",
        "per_hr_timeout": "1m",
        "total_timeout": "5m",
        "subprocess_slack": "5s",
    }
    base.update(overrides)
    return TestRequest(**base)


# ----- request validation --------------------------------------------------


def test_request_rejects_empty_chart_name() -> None:
    with pytest.raises(ChartManagerError):
        TestRequest(chart_name="", version=VERSION)


def test_request_rejects_empty_version() -> None:
    with pytest.raises(ChartManagerError):
        TestRequest(chart_name=CHART, version="")


def test_request_rejects_zero_concurrency() -> None:
    with pytest.raises(ChartManagerError):
        TestRequest(chart_name=CHART, version=VERSION, concurrency=0)


def test_request_rejects_per_hr_below_30s() -> None:
    with pytest.raises(ChartManagerError):
        TestRequest(chart_name=CHART, version=VERSION, per_hr_timeout="10s")


def test_request_rejects_total_lt_per_hr() -> None:
    with pytest.raises(ChartManagerError):
        TestRequest(
            chart_name=CHART, version=VERSION, per_hr_timeout="5m", total_timeout="1m"
        )


def test_request_rejects_subprocess_slack_below_5s() -> None:
    with pytest.raises(ChartManagerError):
        TestRequest(chart_name=CHART, version=VERSION, subprocess_slack="1s")


def test_request_rejects_pod_log_tail_below_one() -> None:
    with pytest.raises(ChartManagerError):
        TestRequest(chart_name=CHART, version=VERSION, pod_log_tail=0)


def test_request_rejects_stdout_max_bytes_below_minimum() -> None:
    with pytest.raises(ChartManagerError):
        TestRequest(chart_name=CHART, version=VERSION, helm_test_stdout_max_bytes=10)


# ----- top-level flow ------------------------------------------------------


def test_zero_match_returns_synthetic_no_match_outcome() -> None:
    a = _ref("a", "ns1")
    b = _ref("b", "ns2")
    flux = _FakeFlux(
        list_result=[a, b],
        statuses={
            ("ns1", "a"): _released_status(a, chart_name="other"),
            ("ns2", "b"): _released_status(b, version="9.9.9"),
        },
    )
    result = _make_service(flux).test(_req())
    assert len(result.outcomes) == 1
    [o] = result.outcomes
    assert o.verdict == "no-match"
    assert o.reason == "NoHelmReleasesMatched"
    assert result.ok is False


# ----- preflight ------------------------------------------------------------


def test_suspended_short_circuits_no_helm_no_delete_no_events() -> None:
    ref = _ref()
    s = _released_status(ref, suspended=True)
    flux = _FakeFlux(list_result=[ref], statuses={("loki", "loki"): s})
    helm = _FakeHelm()
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "skipped-suspended"
    assert helm.calls == []
    assert not any(c[0] == "delete_pod" for c in flux.calls)
    assert not any(c[0] == "namespace_events" for c in flux.calls)


def test_not_released_skips_without_helm() -> None:
    ref = _ref()
    s = HelmReleaseStatus(
        ref=ref,
        observed_at=WALL_BASE,
        generation=1,
        observed_generation=1,
        resource_version="1",
        suspended=False,
        desired_chart_name=CHART,
        desired_chart_version=VERSION,
        last_applied_revision=None,
        history_chart_version=VERSION,
        conditions=(_cond("Ready", "Unknown", reason="Progressing"),),
    )
    flux = _FakeFlux(list_result=[ref], statuses={("loki", "loki"): s})
    helm = _FakeHelm()
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "skipped-not-ready"
    assert o.reason == "NotReleased"
    assert helm.calls == []


def test_generation_lag_skips_without_helm() -> None:
    ref = _ref()
    s = _released_status(ref, generation=2, observed_generation=1)
    flux = _FakeFlux(list_result=[ref], statuses={("loki", "loki"): s})
    helm = _FakeHelm()
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "skipped-not-ready"
    assert o.reason == "GenerationLag"
    assert helm.calls == []


# ----- reaping --------------------------------------------------------------


def test_reap_succeeded_pods_then_run_helm() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
        test_pods={
            ("loki", "loki"): [("loki", "loki-test-old", "Succeeded")],
        },
    )
    helm = _FakeHelm(result=_ok_result())
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "passed"
    delete_calls = [c for c in flux.calls if c[0] == "delete_pod"]
    assert len(delete_calls) == 1
    assert delete_calls[0][1] == ("loki", "loki-test-old")
    assert len(helm.calls) == 1


def test_running_pod_refuses_run() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
        test_pods={("loki", "loki"): [("loki", "loki-test-live", "Running")]},
    )
    helm = _FakeHelm()
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert o.reason == "TestPodInFlight"
    assert "loki-test-live" in (o.diagnostics or "")
    assert helm.calls == []
    assert not any(c[0] == "delete_pod" for c in flux.calls)


def test_unknown_phase_refuses_run() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
        test_pods={("loki", "loki"): [("loki", "loki-test-x", "Unknown")]},
    )
    helm = _FakeHelm()
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert o.reason == "TestPodInFlight"
    assert helm.calls == []


def test_empty_phase_refuses_run() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
        test_pods={("loki", "loki"): [("loki", "loki-test-y", "")]},
    )
    helm = _FakeHelm()
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert o.reason == "TestPodInFlight"
    assert helm.calls == []


def test_mixed_running_and_succeeded_refuses_no_delete() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
        test_pods={
            ("loki", "loki"): [
                ("loki", "loki-test-old", "Succeeded"),
                ("loki", "loki-test-live", "Running"),
            ]
        },
    )
    helm = _FakeHelm()
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert o.reason == "TestPodInFlight"
    assert not any(c[0] == "delete_pod" for c in flux.calls)
    assert helm.calls == []


def test_partial_reap_failure_yields_reap_incomplete() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
        test_pods={
            ("loki", "loki"): [
                ("loki", "loki-test-old", "Succeeded"),
                ("loki", "loki-test-old2", "Failed"),
            ]
        },
        delete_failures={("loki", "loki-test-old2")},
    )
    helm = _FakeHelm()
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert o.reason == "ReapIncomplete"
    assert "loki-test-old2" in (o.diagnostics or "")
    assert helm.calls == []


# ----- helm classification --------------------------------------------------


def test_helm_rc0_passes_without_namespace_events() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
        namespace_events_exc=ExternalCommandError("would blow up", stderr="x"),
    )
    helm = _FakeHelm(result=_ok_result())
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "passed"
    assert o.reason == "AllTestsPassed"
    assert o.diagnostics is None
    assert not any(c[0] == "namespace_events" for c in flux.calls)


def test_helm_no_tests_to_run_classified_as_passed_no_diagnostics() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
        namespace_events_exc=ExternalCommandError("nope", stderr="nope"),
    )
    helm = _FakeHelm(
        result=_bad_result("Error: no tests to run for chart loki")
    )
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "passed"
    assert o.reason == "NoTestsDefined"
    assert o.diagnostics is None
    assert not any(c[0] == "namespace_events" for c in flux.calls)


def test_helm_already_exists_classified_as_test_pod_conflict() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
    )
    helm = _FakeHelm(
        result=_bad_result("Error: pod cert-manager-test already exists")
    )
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert o.reason == "TestPodConflict"
    assert o.diagnostics is not None


def test_helm_cluster_unreachable_classified_as_helm_unavailable() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
    )
    helm = _FakeHelm(result=_bad_result("Error: cluster unreachable"))
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert o.reason == "HelmUnavailable"


def test_helm_generic_failure_includes_pod_logs_in_diagnostics() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
        # Reaping returns no pods; refresh after failure returns one pod.
        test_pods={("loki", "loki"): [("loki", "loki-test-fresh", "Failed")]},
        pod_logs_map={("loki", "loki-test-fresh", False): "log content"},
    )
    helm = _FakeHelm(result=_bad_result("Error: bare failure"))
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert o.reason == "TestFailed"
    assert o.diagnostics is not None
    assert "log content" in o.diagnostics
    # First list_test_pods is the reap; second is from diagnostics composition.
    assert flux.list_test_pods_count == 2


# ----- pod log fallback semantics ------------------------------------------


def test_previous_logs_fallback_fires_for_terminal_phase_empty_logs() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
        test_pods={("loki", "loki"): [("loki", "loki-test", "Failed")]},
        pod_logs_map={
            ("loki", "loki-test", False): "",
            ("loki", "loki-test", True): "previous boom",
        },
    )
    helm = _FakeHelm(result=_bad_result("Error: bare failure"))
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    pod_log_calls = [c for c in flux.calls if c[0] == "pod_logs"]
    previous_calls = [c for c in pod_log_calls if c[2]["previous"]]
    assert len(previous_calls) == 1
    assert "previous boom" in (o.diagnostics or "")


def test_previous_logs_fallback_does_not_fire_for_running_pod() -> None:
    ref = _ref()
    # Test pods listed during diagnostics include a Running pod (this is
    # different from reap-time; we only refresh after helm has failed).
    # Reap sees empty so we proceed; helm fails; diagnostics snapshot sees
    # the Running pod with empty logs.
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
        test_pods={("loki", "loki"): [("loki", "loki-test", "Running")]},
        pod_logs_map={("loki", "loki-test", False): ""},
    )
    # Make reap see nothing by using a different mechanism: it sees the
    # Running pod and refuses. Instead, override list_test_pods to return
    # different on second call.
    seq: list[list[tuple[str, str, str]]] = [
        [],  # reap sees nothing
        [("loki", "loki-test", "Running")],  # diagnostics sees Running
    ]

    def list_pods(_ref: HelmReleaseRef, *, timeout: float | None = None) -> list[tuple[str, str, str]]:
        flux.calls.append(("list_test_pods", (_ref,), {"timeout": timeout}))
        return seq.pop(0)

    flux.list_test_pods = list_pods  # type: ignore[assignment]
    helm = _FakeHelm(result=_bad_result("Error: bare failure"))
    _make_service(flux, helm).test(_req())
    previous_calls = [c for c in flux.calls if c[0] == "pod_logs" and c[2]["previous"]]
    assert previous_calls == []


# ----- timeouts -------------------------------------------------------------


def test_helm_timeout_raises_yields_per_hr_budget_exhausted() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
    )
    helm = _FakeHelm(result=ExternalCommandError("command timed out after 30s\nblah"))
    # Clock stays low so total deadline isn't tripped.
    result = _make_service(flux, helm, clock=_Clock()).test(_req())
    [o] = result.outcomes
    assert o.verdict == "timed-out"
    assert o.reason == "PerHRBudgetExhausted"


def test_total_timeout_cancels_remaining_outcomes() -> None:
    refs = [_ref(f"a{i}", "ns") for i in range(3)]
    flux = _FakeFlux(
        list_result=refs,
        statuses={(r.namespace, r.name): _released_status(r) for r in refs},
    )

    # Fake helm: returns ok but only the first call records before the
    # clock trips the total budget. Subsequent calls happen after deadline,
    # but they still succeed at the helm level -- we rely on `total_timed_out`
    # plus the cancel_event being set.
    barrier = threading.Event()

    def helm_test(*args: Any, **kwargs: Any) -> CommandResult:
        # First call returns immediately; later calls block briefly so the
        # collection loop can observe the clock crossing.
        if not barrier.is_set():
            barrier.set()
            return _ok_result()
        return _ok_result()

    helm = _FakeHelm()
    helm.result_fn = lambda r, n: helm_test(r, n)

    # Clock starts at 0; total_timeout is 5m = 300s. Step 250s per tick
    # so total deadline (300) is exceeded within a couple of as_completed
    # iterations.
    clock = _Clock(start=0.0, step=250.0)
    result = _make_service(flux, helm, clock=clock).test(_req(concurrency=3))
    assert result.total_timed_out is True


# ----- concurrency ----------------------------------------------------------


def test_concurrency_fan_out_runs_in_parallel() -> None:
    refs = [_ref(f"a{i}", "ns") for i in range(3)]
    flux = _FakeFlux(
        list_result=refs,
        statuses={(r.namespace, r.name): _released_status(r) for r in refs},
    )
    ready = threading.Barrier(3, timeout=2)

    def helm_test(_release: str, _ns: str) -> CommandResult:
        ready.wait()
        return _ok_result()

    helm = _FakeHelm()
    helm.result_fn = helm_test
    _make_service(flux, helm).test(_req(concurrency=3))
    assert len(set(helm.threads)) >= 2


# ----- progress callback ---------------------------------------------------


def test_progress_callback_phase_sequence_passed() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
    )
    helm = _FakeHelm(result=_ok_result())
    seen: list[tuple[str, str]] = []
    _make_service(
        flux, helm, progress=lambda _ref, t: seen.append((_ref.name, t.phase))
    ).test(_req())
    phases = [p for _, p in seen]
    assert phases == ["Preflight", "Reaping", "Running", "Finished"]


def test_progress_callback_phase_sequence_suspended() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref, suspended=True)},
    )
    seen: list[str] = []
    _make_service(
        flux, _FakeHelm(), progress=lambda _ref, t: seen.append(t.phase)
    ).test(_req())
    assert seen == ["Preflight"]


def test_progress_callback_phase_sequence_in_flight() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
        test_pods={("loki", "loki"): [("loki", "x", "Running")]},
    )
    seen: list[str] = []
    _make_service(
        flux, _FakeHelm(), progress=lambda _ref, t: seen.append(t.phase)
    ).test(_req())
    assert seen == ["Preflight", "Reaping"]


def test_progress_callback_phase_sequence_test_failed() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
    )
    helm = _FakeHelm(result=_bad_result("Error: bare failure"))
    seen: list[str] = []
    _make_service(
        flux, helm, progress=lambda _ref, t: seen.append(t.phase)
    ).test(_req())
    assert seen == ["Preflight", "Reaping", "Running", "Finished"]


def test_progress_callback_that_raises_does_not_break_service() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
    )
    helm = _FakeHelm(result=_ok_result())

    def explode(_ref: HelmReleaseRef, _t: Transition) -> None:
        raise RuntimeError("callback boom")

    result = _make_service(flux, helm, progress=explode).test(_req())
    [o] = result.outcomes
    assert o.verdict == "passed"


# ----- re-run idempotence --------------------------------------------------


def test_rerun_reaps_round_one_succeeded_pods() -> None:
    ref = _ref()
    # First run: no pods, helm passes. Second run: stale pod from previous
    # run, gets reaped, helm passes again.
    seq: list[list[tuple[str, str, str]]] = [
        [],  # first run reap
        [("loki", "loki-test-round1", "Succeeded")],  # second run reap
    ]

    def list_pods(_ref: HelmReleaseRef, *, timeout: float | None = None) -> list[tuple[str, str, str]]:
        return seq.pop(0)

    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
    )
    flux.list_test_pods = list_pods  # type: ignore[assignment]
    helm = _FakeHelm(result=_ok_result())
    service = _make_service(flux, helm)

    first = service.test(_req())
    second = service.test(_req())
    assert [o.verdict for o in first.outcomes] == ["passed"]
    assert [o.verdict for o in second.outcomes] == ["passed"]
    deletes = [c for c in flux.calls if c[0] == "delete_pod"]
    assert deletes == [("delete_pod", ("loki", "loki-test-round1"), {"timeout": 10.0})]


# ----- diagnostics events fallback -----------------------------------------


def test_namespace_events_failure_does_not_break_outcome() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
        namespace_events_exc=ExternalCommandError("events boom", stderr="boom"),
    )
    helm = _FakeHelm(result=_bad_result("Error: bare failure"))
    result = _make_service(flux, helm).test(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert "<events unavailable" in (o.diagnostics or "")


# ----- mid-reap deadline ---------------------------------------------------


def test_total_deadline_trips_between_reap_and_helm_skips_helm() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
    )
    helm = _FakeHelm(result=_ok_result())
    # Clock advances 200s per call. total_timeout=5m=300s. After a handful
    # of get_status/list_test_pods calls the clock exceeds 300s before
    # _run_helm sees the deadline check.
    clock = _Clock(start=0.0, step=200.0)
    result = _make_service(flux, helm, clock=clock).test(_req())
    [o] = result.outcomes
    assert o.verdict == "timed-out"
    assert o.reason == "TotalBudgetExhausted"
    assert helm.calls == []


# ----- Helm.test signature back-compat ------------------------------------


def test_helm_test_called_with_logs_and_subprocess_timeout() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): _released_status(ref)},
    )
    helm = _FakeHelm(result=_ok_result())
    _make_service(flux, helm).test(_req())
    assert len(helm.calls) == 1
    (release,), kwargs = helm.calls[0]
    assert release == "loki"
    assert kwargs["namespace"] == "loki"
    assert kwargs["timeout"] == "1m"
    assert kwargs["logs"] is True
    assert isinstance(kwargs["subprocess_timeout"], float)
    assert kwargs["subprocess_timeout"] > 0
