from __future__ import annotations

import contextlib
import itertools
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from chart_manager.integrations.flux import (
    ConditionSnapshot,
    HelmReleaseRef,
    HelmReleaseStatus,
    OwnedWorkload,
    WorkloadRollout,
)
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.helmrelease.monitor import (
    MonitorRequest,
    MonitorService,
    Transition,
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
    last_transition_time: datetime | None = None,
) -> ConditionSnapshot:
    return ConditionSnapshot(
        type=type_,
        status=status,
        reason=reason,
        message=message,
        last_transition_time=last_transition_time,
    )


def _status(
    ref: HelmReleaseRef,
    *,
    generation: int = 1,
    observed_generation: int = 1,
    suspended: bool = False,
    desired_chart_name: str | None = CHART,
    desired_chart_version: str | None = VERSION,
    history_chart_version: str | None = VERSION,
    conditions: tuple[ConditionSnapshot, ...] = (),
    observed_at: datetime = WALL_BASE,
) -> HelmReleaseStatus:
    return HelmReleaseStatus(
        ref=ref,
        observed_at=observed_at,
        generation=generation,
        observed_generation=observed_generation,
        resource_version="1",
        suspended=suspended,
        desired_chart_name=desired_chart_name,
        desired_chart_version=desired_chart_version,
        last_applied_revision=None,
        history_chart_version=history_chart_version,
        conditions=conditions,
    )


def _ready(transition_at: datetime, reason: str = "ReconciliationSucceeded") -> ConditionSnapshot:
    return _cond(
        "Ready", "True", reason=reason, message="ok", last_transition_time=transition_at
    )


def _workload(name: str = "loki-app", *, converged: bool = True) -> WorkloadRollout:
    return WorkloadRollout(
        workload=OwnedWorkload(
            kind="Deployment",
            namespace="loki",
            name=name,
            desired=1,
            ready=1 if converged else 0,
            available=1 if converged else 0,
        ),
        converged=converged,
        generation=1,
        observed_generation=1 if converged else 0,
    )


@dataclass
class _FakeFlux:
    """Configurable flux double. Records all calls; per-method scripted behavior."""

    list_result: list[HelmReleaseRef] = field(default_factory=list)
    list_exc: BaseException | None = None
    statuses: dict[tuple[str, str], list[HelmReleaseStatus | BaseException]] = field(
        default_factory=dict
    )
    workloads: dict[tuple[str, str], list[tuple[WorkloadRollout, ...] | BaseException]] = field(
        default_factory=dict
    )
    namespace_events_result: str = "ns evt"
    workload_events_result: str = "wl evt"
    namespace_events_raises: bool = False
    workload_events_raises: bool = False

    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    threads: list[int] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def list(
        self, *, namespace: str | None = None, timeout: float | None = None
    ) -> list[HelmReleaseRef]:
        self.calls.append(("list", (), {"namespace": namespace, "timeout": timeout}))
        if self.list_exc is not None:
            raise self.list_exc
        return list(self.list_result)

    def get_status(
        self, ref: HelmReleaseRef, *, timeout: float | None = None
    ) -> HelmReleaseStatus:
        with self.lock:
            self.threads.append(threading.get_ident())
        self.calls.append(("get_status", (ref,), {"timeout": timeout}))
        key = (ref.namespace, ref.name)
        seq = self.statuses.get(key, [])
        if not seq:
            raise AssertionError(f"no scripted status for {key}")
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def list_owned_workloads(
        self, ref: HelmReleaseRef, *, timeout: float | None = None
    ) -> tuple[WorkloadRollout, ...]:
        self.calls.append(("list_owned_workloads", (ref,), {"timeout": timeout}))
        key = (ref.namespace, ref.name)
        seq = self.workloads.get(key, [])
        if not seq:
            return ()
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, BaseException):
            raise item
        return tuple(item)

    def namespace_events(self, namespace: str, *, timeout: float | None = None) -> str:
        self.calls.append(("namespace_events", (namespace,), {"timeout": timeout}))
        if self.namespace_events_raises:
            raise ExternalCommandError("ns events boom", stderr="ns events boom")
        return self.namespace_events_result

    def workload_events(
        self, kind: str, namespace: str, name: str, *, timeout: float | None = None
    ) -> str:
        self.calls.append(
            ("workload_events", (kind, namespace, name), {"timeout": timeout})
        )
        if self.workload_events_raises:
            raise ExternalCommandError("wl events boom", stderr="wl events boom")
        return self.workload_events_result


class _Clock:
    """Deterministic monotonic clock. Each call advances by `step`."""

    def __init__(self, start: float = 0.0, step: float = 0.0) -> None:
        self.t = start
        self.step = step

    def __call__(self) -> float:
        v = self.t
        self.t += self.step
        return v


class _StepClock:
    """Returns 0.0 for the first `warmup` calls, then advances by `step`.

    Lets a test set up an arbitrary number of watcher polls before the budget
    starts being consumed.
    """

    def __init__(self, warmup: int, step: float) -> None:
        self.warmup = warmup
        self.step = step
        self.calls = 0
        self.t = 0.0

    def __call__(self) -> float:
        self.calls += 1
        if self.calls <= self.warmup:
            return 0.0
        v = self.t
        self.t += self.step
        return v


class _Wall:
    """Deterministic wall clock."""

    def __init__(self, base: datetime = WALL_BASE) -> None:
        self.base = base

    def __call__(self) -> datetime:
        return self.base


def _make_service(
    flux: _FakeFlux,
    *,
    clock: Callable[[], float] | None = None,
    now: Callable[[], datetime] | None = None,
    rand: Callable[[float, float], float] = lambda lo, hi: 0.0,
    progress: Callable[[HelmReleaseRef, Transition], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> MonitorService:
    return MonitorService(
        flux,  # type: ignore[arg-type]
        sleep=sleep or (lambda _t: None),
        clock=clock or _Clock(),
        now=now or _Wall(),
        rand=rand,
        progress=progress,
    )


def _req(**overrides: Any) -> MonitorRequest:
    base: dict[str, Any] = {
        "chart_name": CHART,
        "version": VERSION,
        "per_poll_timeout": "10s",
        "per_hr_timeout": "5m",
        "total_timeout": "15m",
        "poll_interval": 1.0,
        "recent_transitions_size": 5,
        "concurrency": 2,
    }
    base.update(overrides)
    return MonitorRequest(**base)


# ----- request validation --------------------------------------------------


def test_request_validation_rejects_empty_chart_name() -> None:
    with pytest.raises(ChartManagerError):
        MonitorRequest(chart_name="", version=VERSION)


def test_request_validation_rejects_per_hr_lt_poll_interval() -> None:
    with pytest.raises(ChartManagerError):
        MonitorRequest(
            chart_name=CHART,
            version=VERSION,
            poll_interval=10.0,
            per_hr_timeout="1s",
            total_timeout="2s",
        )


def test_request_validation_rejects_total_lt_per_hr() -> None:
    with pytest.raises(ChartManagerError):
        MonitorRequest(
            chart_name=CHART,
            version=VERSION,
            per_hr_timeout="10m",
            total_timeout="1m",
        )


def test_request_validation_rejects_zero_concurrency() -> None:
    with pytest.raises(ChartManagerError):
        MonitorRequest(chart_name=CHART, version=VERSION, concurrency=0)


# ----- top-level flow ------------------------------------------------------


def test_flux_list_failure_propagates() -> None:
    flux = _FakeFlux(list_exc=ExternalCommandError("boom", stderr="boom"))
    service = _make_service(flux)
    with pytest.raises(ExternalCommandError):
        service.monitor(_req())


def test_zero_match_returns_synthetic_no_match_outcome() -> None:
    a = _ref("a", "ns1")
    b = _ref("b", "ns2")
    flux = _FakeFlux(
        list_result=[a, b],
        statuses={
            ("ns1", "a"): [_status(a, desired_chart_name="other")],
            ("ns2", "b"): [_status(b, desired_chart_version="9.9.9")],
        },
    )
    result = _make_service(flux).monitor(_req())
    assert len(result.outcomes) == 1
    [o] = result.outcomes
    assert o.verdict == "no-match"
    assert o.reason == "NoHelmReleasesMatched"
    assert result.ok is False


# ----- ready paths ---------------------------------------------------------


def _ready_status(ref: HelmReleaseRef, *, transition_at: datetime = WALL_BASE) -> HelmReleaseStatus:
    return _status(ref, conditions=(_ready(transition_at),))


def test_generation_lag_then_ready() -> None:
    ref = _ref()
    laggy = _status(
        ref,
        generation=2,
        observed_generation=1,
        history_chart_version="0.1.0",
        conditions=(_ready(WALL_BASE),),
    )
    history_lag = _status(
        ref,
        generation=2,
        observed_generation=2,
        history_chart_version="0.1.0",
        conditions=(_ready(WALL_BASE),),
    )
    converged = _status(
        ref,
        generation=2,
        observed_generation=2,
        history_chart_version=VERSION,
        conditions=(_ready(WALL_BASE),),
    )
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): [laggy, history_lag, converged]},
        workloads={("loki", "loki"): [(_workload(),)]},
    )
    result = _make_service(flux).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "ready"
    phases = [t.phase for t in o.recent_transitions]
    assert len(set(phases)) >= 2


def test_stale_ready_with_converged_gen_and_history_is_immediately_ready() -> None:
    ref = _ref()
    stale = _status(
        ref,
        conditions=(_ready(WALL_BASE - timedelta(hours=1)),),
    )
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): [stale]},
        workloads={("loki", "loki"): [(_workload(),)]},
    )
    result = _make_service(flux).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "ready"
    assert all(t.phase != "StaleReady" for t in o.recent_transitions)
    get_status_calls = [c for c in flux.calls if c[0] == "get_status"]
    assert len(get_status_calls) <= 2


def test_no_stale_ready_phase_emitted_anywhere() -> None:
    ref = _ref()
    stale = _status(
        ref,
        conditions=(_ready(WALL_BASE - timedelta(hours=1)),),
    )
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): [stale]},
        workloads={("loki", "loki"): [(_workload(),)]},
    )
    result = _make_service(flux).monitor(_req())
    for o in result.outcomes:
        assert all(t.phase != "StaleReady" for t in o.recent_transitions)
        if o.diagnostics is not None:
            assert "StaleReady" not in o.diagnostics


def test_old_but_healthy_hr_ready_on_first_poll() -> None:
    ref = _ref()
    ancient = _status(
        ref,
        conditions=(_ready(WALL_BASE - timedelta(days=1)),),
    )
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): [ancient]},
        workloads={("loki", "loki"): [(_workload(),)]},
    )
    result = _make_service(flux).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "ready"
    assert all(t.phase != "StaleReady" for t in o.recent_transitions)
    get_status_calls = [c for c in flux.calls if c[0] == "get_status"]
    assert len(get_status_calls) <= 2


def test_history_version_mismatch_blocks_ready() -> None:
    ref = _ref()
    mismatch = _status(
        ref,
        history_chart_version="old",
        conditions=(_ready(WALL_BASE),),
    )
    match = _status(ref, conditions=(_ready(WALL_BASE),))
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): [mismatch, match]},
        workloads={("loki", "loki"): [(_workload(),)]},
    )
    result = _make_service(flux).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "ready"


def test_workload_not_converged_then_converges() -> None:
    ref = _ref()
    s = _status(ref, conditions=(_ready(WALL_BASE),))
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): [s]},
        workloads={
            ("loki", "loki"): [
                (_workload(converged=False),),
                (_workload(converged=False),),
                (_workload(converged=True),),
            ]
        },
    )
    result = _make_service(flux).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "ready"


# ----- terminal failures ---------------------------------------------------


def test_terminal_install_failed_fails_fast() -> None:
    ref = _ref()
    bad = _status(
        ref,
        conditions=(_cond("Ready", "False", reason="InstallFailed", message="bad"),),
    )
    # Second status would be ready -- if monitor called it, the test fails.
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): [bad, _ready_status(ref)]},
    )
    initial_get_status_count = 0
    result = _make_service(flux).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert o.reason == "InstallFailed"
    # Only the upfront filter call should have happened.
    get_status_calls = [c for c in flux.calls if c[0] == "get_status"]
    assert len(get_status_calls) == 1 + initial_get_status_count


def test_retry_exhausted_is_terminal() -> None:
    ref = _ref()
    bad = _status(
        ref,
        conditions=(_cond("Ready", "False", reason="RetryExhausted"),),
    )
    flux = _FakeFlux(list_result=[ref], statuses={("loki", "loki"): [bad]})
    result = _make_service(flux).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert o.reason == "RetryExhausted"


def test_stalled_fails_fast() -> None:
    ref = _ref()
    bad = _status(
        ref,
        conditions=(
            _ready(WALL_BASE),
            _cond("Stalled", "True", message="stuck"),
        ),
    )
    flux = _FakeFlux(list_result=[ref], statuses={("loki", "loki"): [bad]})
    result = _make_service(flux).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert o.reason == "Stalled"


def test_test_success_false_terminal_only_after_released() -> None:
    ref = _ref()
    pre_release = _status(
        ref,
        conditions=(
            _ready(WALL_BASE),
            _cond("TestSuccess", "False", reason="X"),
            _cond("Released", "False"),
        ),
    )
    post_release = _status(
        ref,
        conditions=(
            _ready(WALL_BASE),
            _cond("TestSuccess", "False", reason="TestFailed", message="boom"),
            _cond("Released", "True"),
        ),
    )
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): [pre_release, post_release]},
        workloads={("loki", "loki"): [(_workload(converged=False),)]},
    )
    result = _make_service(flux).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert o.reason == "TestFailed"


# ----- timeouts ------------------------------------------------------------


def test_per_hr_timeout_yields_timed_out() -> None:
    ref = _ref()
    waiting = _status(
        ref,
        generation=2,
        observed_generation=1,
        conditions=(_cond("Ready", "Unknown", reason="Progressing"),),
    )
    flux = _FakeFlux(list_result=[ref], statuses={("loki", "loki"): [waiting]})
    # Per-HR=300s, total=900s. Stay at 0 until the watcher has recorded the
    # initial transition, then jump to a value above per-HR but below total.
    clock = _StepClock(warmup=5, step=400.0)
    service = _make_service(flux, clock=clock)
    result = service.monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "timed-out"
    assert o.reason == "PerHRBudgetExhausted"


def test_total_timeout_cancels_remaining() -> None:
    refs = [_ref(f"a{i}", "ns") for i in range(3)]
    # First HR converges immediately; others stay waiting.
    statuses: dict[tuple[str, str], list[HelmReleaseStatus]] = {
        (refs[0].namespace, refs[0].name): [_ready_status(refs[0])],
        (refs[1].namespace, refs[1].name): [
            _status(
                refs[1],
                generation=2,
                observed_generation=1,
                conditions=(_cond("Ready", "Unknown", reason="Progressing"),),
            )
        ],
        (refs[2].namespace, refs[2].name): [
            _status(
                refs[2],
                generation=2,
                observed_generation=1,
                conditions=(_cond("Ready", "Unknown", reason="Progressing"),),
            )
        ],
    }
    workloads: dict[tuple[str, str], list[tuple[WorkloadRollout, ...]]] = {
        (refs[0].namespace, refs[0].name): [(_workload(),)],
    }
    flux = _FakeFlux(list_result=refs, statuses=statuses, workloads=workloads)
    # Force total timeout: clock advances quickly so total budget (900s) trips
    # after a handful of ticks.
    clock = _Clock(start=0.0, step=500.0)
    service = _make_service(
        flux, clock=clock, sleep=lambda _t: None
    )
    result = service.monitor(_req(concurrency=3))
    verdicts = sorted(o.verdict for o in result.outcomes)
    assert verdicts.count("ready") == 1
    assert verdicts.count("timed-out") == 2
    for o in result.outcomes:
        if o.verdict == "timed-out":
            assert o.reason in ("TotalBudgetExhausted", "PerHRBudgetExhausted")


# ----- suspend -------------------------------------------------------------


def test_suspended_short_circuits() -> None:
    ref = _ref()
    susp = _status(ref, suspended=True, conditions=(_ready(WALL_BASE),))
    flux = _FakeFlux(list_result=[ref], statuses={("loki", "loki"): [susp]})
    flux.namespace_events_raises = True  # would blow up if diagnostics ran
    flux.workload_events_raises = True
    result = _make_service(flux).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "skipped-suspended"
    assert result.ok is True
    get_status_calls = [c for c in flux.calls if c[0] == "get_status"]
    assert len(get_status_calls) == 1
    assert not any(c[0] == "list_owned_workloads" for c in flux.calls)
    assert not any(c[0] == "namespace_events" for c in flux.calls)


# ----- ring dedupe ---------------------------------------------------------


def test_recent_transitions_deduped() -> None:
    ref = _ref()
    progressing = _status(
        ref,
        generation=2,
        observed_generation=1,
        conditions=(_cond("Ready", "Unknown", reason="Progressing"),),
    )
    bumped = _status(
        ref,
        generation=2,
        observed_generation=2,
        history_chart_version="old",
        conditions=(_cond("Ready", "Unknown", reason="Progressing"),),
    )
    seq: list[HelmReleaseStatus] = [progressing] * 10 + [bumped] * 2
    flux = _FakeFlux(list_result=[ref], statuses={("loki", "loki"): seq})
    # Stay at 0 long enough to consume the full status sequence, then jump
    # past per-HR to terminate the watcher.
    clock = _StepClock(warmup=50, step=400.0)
    result = _make_service(flux, clock=clock).monitor(_req())
    [o] = result.outcomes
    phases = [t.phase for t in o.recent_transitions]
    # Initial entry is GenerationLag; after observed catches up but history
    # lags it becomes HistoryLag.
    assert "GenerationLag" in phases
    assert "HistoryLag" in phases
    # No duplicate adjacent entries.
    for a, b in itertools.pairwise(phases):
        assert a != b


# ----- disappeared / poll error -------------------------------------------


def test_disappeared_distinguished_from_transient() -> None:
    ref = _ref()
    initial = _status(
        ref,
        generation=2,
        observed_generation=1,
        conditions=(_cond("Ready", "Unknown", reason="Progressing"),),
    )
    flux = _FakeFlux(
        list_result=[ref],
        statuses={
            ("loki", "loki"): [
                initial,
                ExternalCommandError(
                    "...NotFound...",
                    stderr='Error from server (NotFound): helmreleases.helm.toolkit.fluxcd.io "loki" not found',
                ),
            ]
        },
    )
    result = _make_service(flux).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "failed"
    assert o.reason == "Disappeared"


def test_transient_poll_error_logs_and_continues() -> None:
    ref = _ref()
    initial = _status(
        ref,
        generation=2,
        observed_generation=1,
        conditions=(_cond("Ready", "Unknown", reason="Progressing"),),
    )
    flux = _FakeFlux(
        list_result=[ref],
        statuses={
            ("loki", "loki"): [
                initial,
                ExternalCommandError("flake", stderr="apiserver flake"),
                ExternalCommandError("flake", stderr="apiserver flake"),
            ]
        },
    )
    clock = _Clock(start=0.0, step=200.0)  # trip per-hr after several ticks
    result = _make_service(flux, clock=clock).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "timed-out"
    phases = [t.phase for t in o.recent_transitions]
    assert "PollError" in phases


# ----- diagnostics ---------------------------------------------------------


def test_diagnostics_skipped_for_ready_outcomes() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): [_ready_status(ref)]},
        workloads={("loki", "loki"): [(_workload(),)]},
    )

    def boom_ns(*a: Any, **k: Any) -> str:
        raise AssertionError("namespace_events should not be called for ready outcomes")

    def boom_wl(*a: Any, **k: Any) -> str:
        raise AssertionError("workload_events should not be called for ready outcomes")

    flux.namespace_events = boom_ns  # type: ignore[method-assign]
    flux.workload_events = boom_wl  # type: ignore[method-assign]
    result = _make_service(flux).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "ready"
    assert o.diagnostics is None


def test_diagnostics_composed_for_failed_outcomes() -> None:
    ref = _ref()
    bad = _status(
        ref,
        conditions=(_cond("Ready", "False", reason="InstallFailed", message="bad"),),
    )
    flux = _FakeFlux(list_result=[ref], statuses={("loki", "loki"): [bad]})
    result = _make_service(flux).monitor(_req())
    [o] = result.outcomes
    assert o.diagnostics is not None
    assert "## loki/loki - failed: InstallFailed" in o.diagnostics
    assert "### Status" in o.diagnostics
    assert "### Events (namespace loki)" in o.diagnostics


# ----- progress callback ---------------------------------------------------


def test_progress_callback_invoked_per_distinct_transition() -> None:
    ref = _ref()
    laggy = _status(
        ref,
        generation=2,
        observed_generation=1,
        history_chart_version="old",
        conditions=(_ready(WALL_BASE),),
    )
    history_lag = _status(
        ref,
        generation=2,
        observed_generation=2,
        history_chart_version="old",
        conditions=(_ready(WALL_BASE),),
    )
    final = _status(
        ref,
        generation=2,
        observed_generation=2,
        history_chart_version=VERSION,
        conditions=(_ready(WALL_BASE),),
    )
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): [laggy, history_lag, final]},
        workloads={("loki", "loki"): [(_workload(),)]},
    )
    calls: list[tuple[HelmReleaseRef, Transition]] = []
    result = _make_service(
        flux, progress=lambda r, t: calls.append((r, t))
    ).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "ready"
    assert len(calls) == len(o.recent_transitions)
    assert len({t.phase for _, t in calls}) >= 2


def test_progress_callback_exception_does_not_break_watcher() -> None:
    ref = _ref()
    flux = _FakeFlux(
        list_result=[ref],
        statuses={("loki", "loki"): [_ready_status(ref)]},
        workloads={("loki", "loki"): [(_workload(),)]},
    )

    def bad(_ref: HelmReleaseRef, _t: Transition) -> None:
        raise RuntimeError("callback crash")

    result = _make_service(flux, progress=bad).monitor(_req())
    [o] = result.outcomes
    assert o.verdict == "ready"


# ----- timeout plumbing ----------------------------------------------------


def test_per_poll_timeout_plumbed_to_flux_calls() -> None:
    ref = _ref()
    bad = _status(
        ref,
        conditions=(_cond("Ready", "False", reason="InstallFailed", message="m"),),
    )
    flux = _FakeFlux(list_result=[ref], statuses={("loki", "loki"): [bad]})
    _make_service(flux).monitor(_req(per_poll_timeout="7s"))
    for name, _args, kwargs in flux.calls:
        if name in ("list", "get_status", "list_owned_workloads", "namespace_events", "workload_events"):
            assert kwargs.get("timeout") == 7.0


# ----- concurrency / jitter / ordering ------------------------------------


def test_concurrency_runs_in_parallel() -> None:
    refs = [_ref(f"r{i}", "ns") for i in range(3)]
    # First-tick (initial_status) is gen-lag so watcher must re-fetch; the
    # re-fetch is what runs on the pool thread.
    statuses: dict[tuple[str, str], list[HelmReleaseStatus]] = {}
    workloads: dict[tuple[str, str], list[tuple[WorkloadRollout, ...]]] = {}
    for ref in refs:
        laggy = _status(
            ref,
            generation=2,
            observed_generation=1,
            conditions=(_cond("Ready", "Unknown", reason="Progressing"),),
        )
        statuses[(ref.namespace, ref.name)] = [laggy, _ready_status(ref)]
        workloads[(ref.namespace, ref.name)] = [(_workload(),)]
    flux = _FakeFlux(list_result=refs, statuses=statuses, workloads=workloads)
    barrier = threading.Barrier(3, timeout=5)
    orig_get_status = flux.get_status
    second_call_threads: list[int] = []
    call_count = {"n": 0}
    cc_lock = threading.Lock()

    def get_status(ref: HelmReleaseRef, *, timeout: float | None = None) -> HelmReleaseStatus:
        result = orig_get_status(ref, timeout=timeout)
        with cc_lock:
            call_count["n"] += 1
            n = call_count["n"]
        # The first 3 calls are the upfront filter pass (sequential). The
        # next 3 are watcher re-fetches, which should overlap on the pool.
        if n > 3:
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait()
            with cc_lock:
                second_call_threads.append(threading.get_ident())
        return result

    flux.get_status = get_status  # type: ignore[method-assign]
    service = _make_service(flux)
    service.monitor(_req(concurrency=3))
    assert len(set(second_call_threads)) >= 2


def test_jitter_applied_once_before_first_tick() -> None:
    refs = [_ref(f"j{i}", "ns") for i in range(3)]
    statuses: dict[tuple[str, str], list[HelmReleaseStatus]] = {}
    workloads: dict[tuple[str, str], list[tuple[WorkloadRollout, ...]]] = {}
    for ref in refs:
        statuses[(ref.namespace, ref.name)] = [_ready_status(ref)]
        workloads[(ref.namespace, ref.name)] = [(_workload(),)]
    flux = _FakeFlux(list_result=refs, statuses=statuses, workloads=workloads)
    rand_calls: list[tuple[float, float]] = []

    def rand(lo: float, hi: float) -> float:
        rand_calls.append((lo, hi))
        return 0.0

    service = _make_service(flux, rand=rand)
    service.monitor(_req(concurrency=3, poll_interval=2.0))
    assert len(rand_calls) == 3
    assert all(call == (0.0, 2.0) for call in rand_calls)


def test_outcomes_sorted_by_ns_name() -> None:
    refs = [_ref("zzz", "b"), _ref("aaa", "a"), _ref("mmm", "a")]
    statuses: dict[tuple[str, str], list[HelmReleaseStatus]] = {}
    workloads: dict[tuple[str, str], list[tuple[WorkloadRollout, ...]]] = {}
    for ref in refs:
        statuses[(ref.namespace, ref.name)] = [_ready_status(ref)]
        workloads[(ref.namespace, ref.name)] = [(_workload(),)]
    flux = _FakeFlux(list_result=refs, statuses=statuses, workloads=workloads)
    result = _make_service(flux).monitor(_req(concurrency=3))
    assert [(o.ref.namespace, o.ref.name) for o in result.outcomes] == [
        ("a", "aaa"),
        ("a", "mmm"),
        ("b", "zzz"),
    ]


# ----- fail_fast ----------------------------------------------------------


def test_fail_fast_cancels_in_flight_watchers_on_first_failure() -> None:
    """With fail_fast, a failed outcome should cancel the rest mid-flight.

    Two HRs: one fails immediately on the initial status; the other would
    have been ready after polling. Concurrency=1 forces sequential execution
    so the failure resolves first; the second's first cancellation gate
    must short-circuit to a timed-out / TotalBudgetExhausted outcome.
    """
    fail_ref = _ref("aaa", "ns")
    slow_ref = _ref("zzz", "ns")
    bad = _status(
        fail_ref,
        conditions=(_cond("Ready", "False", reason="InstallFailed", message="bad"),),
    )
    slow_ready = _ready_status(slow_ref)
    flux = _FakeFlux(
        list_result=[fail_ref, slow_ref],
        statuses={
            ("ns", "aaa"): [bad],
            ("ns", "zzz"): [slow_ready],
        },
        workloads={("ns", "zzz"): [(_workload(),)]},
    )
    result = _make_service(flux).monitor(_req(concurrency=1, fail_fast=True))
    by_name = {(o.ref.namespace, o.ref.name): o for o in result.outcomes}
    assert by_name[("ns", "aaa")].verdict == "failed"
    assert by_name[("ns", "zzz")].verdict == "timed-out"
    assert by_name[("ns", "zzz")].reason == "TotalBudgetExhausted"


def test_fail_fast_disabled_lets_subsequent_watchers_complete() -> None:
    fail_ref = _ref("aaa", "ns")
    ok_ref = _ref("zzz", "ns")
    bad = _status(
        fail_ref,
        conditions=(_cond("Ready", "False", reason="InstallFailed"),),
    )
    ok = _ready_status(ok_ref)
    flux = _FakeFlux(
        list_result=[fail_ref, ok_ref],
        statuses={
            ("ns", "aaa"): [bad],
            ("ns", "zzz"): [ok],
        },
        workloads={("ns", "zzz"): [(_workload(),)]},
    )
    result = _make_service(flux).monitor(_req(concurrency=1, fail_fast=False))
    by_name = {(o.ref.namespace, o.ref.name): o for o in result.outcomes}
    assert by_name[("ns", "aaa")].verdict == "failed"
    assert by_name[("ns", "zzz")].verdict == "ready"
