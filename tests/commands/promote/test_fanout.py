"""`run_fanout` and `run_matched`: concurrency, crashes, Ctrl-C and timing.

Also what `monitor` and `test` share: request checks, no-match and progress. Sorting,
telemetry and the timed-out flag are covered through `monitor.run` and `test.run`
(test_monitor.py, test_telemetry.py).
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from chart_manager.commands.promote.fanout import run_fanout, run_matched
from chart_manager.commands.promote.monitor import MonitorRequest
from chart_manager.commands.promote.state import NO_MATCH_REF, Stage, Verdict
from chart_manager.commands.promote.telemetry import PromotionTelemetry
from chart_manager.commands.promote.test import TestRequest
from chart_manager.integrations.kubectl import HelmReleaseRef, HelmReleaseStatus
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.plumbing.progress import ProgressEvent, RowUpdate
from chart_manager.shared.events.writer import EventWriter
from tests.commands.promote.conftest import (
    CHART,
    VERSION,
    cluster,
    helmrelease,
    run_helm_test,
    run_monitor,
)
from tests.conftest import EventLog

_BAD_BOUNDS = [
    ({"chart_name": ""}, "chart_name"),
    ({"version": ""}, "version"),
    ({"concurrency": 0}, "concurrency"),
    ({"per_hr_timeout_seconds": 600.0, "total_timeout_seconds": 60.0}, "total_timeout"),
    ({"per_poll_timeout_seconds": float("nan")}, "per_poll_timeout_seconds"),
    ({"per_hr_timeout_seconds": float("nan")}, "per_hr_timeout_seconds"),
    ({"total_timeout_seconds": float("nan")}, "total_timeout_seconds"),
    ({"per_hr_timeout_seconds": "5m"}, "per_hr_timeout_seconds must be a number"),
]


@pytest.mark.parametrize(
    ("request_type", "fields", "message"),
    [
        *[(t, f, m) for t in (MonitorRequest, TestRequest) for f, m in _BAD_BOUNDS],
        (TestRequest, {"pod_log_tail": 0}, "pod_log_tail"),
    ],
)
def test_the_request_rejects_bad_bounds(
    request_type: type[MonitorRequest | TestRequest], fields: dict[str, Any], message: str
) -> None:
    with pytest.raises(ChartManagerError, match=message):
        request_type(**{"chart_name": CHART, "version": VERSION, **fields})


@pytest.mark.parametrize(("request_type", "floor"), [(MonitorRequest, 3.0), (TestRequest, 30.0)])
def test_the_per_hr_budget_has_a_floor(
    request_type: type[MonitorRequest | TestRequest], floor: float
) -> None:
    request_type(CHART, VERSION, per_hr_timeout_seconds=floor, total_timeout_seconds=floor)
    with pytest.raises(ChartManagerError, match=f"must be >= {floor:g}s"):
        request_type(CHART, VERSION, per_hr_timeout_seconds=floor - 0.5)


STAGES = pytest.mark.parametrize("stage", [run_monitor, run_helm_test])


@STAGES
def test_nothing_matched_is_one_no_match_outcome(stage: Callable[..., Any]) -> None:
    runner = cluster(
        helmrelease("a", "ns1", chart="other"), helmrelease("b", "ns2", version="9.9.9")
    )

    result = stage(runner)

    [outcome] = result.outcomes
    assert (outcome.verdict, outcome.reason) == ("no-match", "NoHelmReleasesMatched")
    assert result.ok is False


@STAGES
def test_a_raising_progress_callback_does_not_break_the_run(stage: Callable[..., Any]) -> None:
    def explode(_event: ProgressEvent | RowUpdate) -> None:
        raise RuntimeError("callback crash")

    assert stage(cluster(helmrelease()), progress=explode).ok is True


def _ref(name: str, namespace: str = "loki") -> HelmReleaseRef:
    return HelmReleaseRef(
        name=name,
        namespace=namespace,
        api_version="helm.toolkit.fluxcd.io/v2",
        release_name=name,
        storage_namespace=namespace,
        target_namespace=namespace,
    )


@dataclass(frozen=True)
class _Outcome:
    """Minimal `HasRef` structural match; the module only reads `.ref`."""

    ref: HelmReleaseRef


def _status(ref: HelmReleaseRef) -> HelmReleaseStatus:
    return HelmReleaseStatus(
        ref=ref,
        observed_at=None,
        generation=1,
        observed_generation=1,
        resource_version="1",
        suspended=False,
        desired_chart_name="loki",
        desired_chart_version="0.2.0",
        last_applied_revision=None,
        history_chart_version="0.2.0",
        conditions=(),
    )


def _raiser(exc: BaseException) -> Callable[[HelmReleaseStatus], _Outcome]:
    def work(_status: HelmReleaseStatus) -> _Outcome:
        raise exc

    return work


def _run(work: Callable[[HelmReleaseStatus], _Outcome]) -> threading.Event:
    """Fan one release out to `work`; return the cancel flag it left behind."""
    cancel_event = threading.Event()
    run_fanout(
        [_status(_ref("loki"))],
        concurrency=2,
        clock=lambda: 0.0,
        total_deadline=1_000.0,
        cancel_event=cancel_event,
        outcomes=[],
        work=work,
        crash_label="test watcher",
    )
    return cancel_event


def test_workers_run_concurrently_up_to_the_bound() -> None:
    # Three workers that each wait for the other two can only finish in parallel.
    barrier = threading.Barrier(3, timeout=5)

    def work(status: HelmReleaseStatus) -> _Outcome:
        barrier.wait()
        return _Outcome(status.ref)

    outcomes: list[_Outcome] = []
    run_fanout(
        [_status(_ref(f"r{i}")) for i in range(3)],
        concurrency=3,
        clock=lambda: 0.0,
        total_deadline=1_000.0,
        cancel_event=threading.Event(),
        outcomes=outcomes,
        work=work,
        crash_label="test watcher",
    )

    assert len(outcomes) == 3


def test_keyboard_interrupt_propagates_unwrapped_and_cancels_peers() -> None:
    """Ctrl-C must stay Ctrl-C all the way out of the fan-out.

    Wrapping it in `ChartManagerError` made it an `Exception`, which both
    stages catch to close their telemetry interval -- a network write
    standing between the operator's Ctrl-C and the process exiting 130.
    """
    cancel_event = threading.Event()

    with pytest.raises(KeyboardInterrupt):
        run_fanout(
            [_status(_ref("loki"))],
            concurrency=2,
            clock=lambda: 0.0,
            total_deadline=1_000.0,
            cancel_event=cancel_event,
            outcomes=[],
            work=_raiser(KeyboardInterrupt()),
            crash_label="test watcher",
        )

    # Peers still get told to stop: an interrupted run must not leave
    # watchers polling a cluster nobody is waiting on.
    assert cancel_event.is_set()


def test_an_unexpected_exception_is_still_wrapped_as_a_crash() -> None:
    with pytest.raises(ChartManagerError, match="test watcher crashed"):
        _run(_raiser(ValueError("boom")))


def test_an_external_command_error_propagates_as_itself() -> None:
    # Infrastructure failure, not a release verdict: the caller needs the
    # original error to render what the cluster said.
    with pytest.raises(ExternalCommandError):
        _run(_raiser(ExternalCommandError("kubectl exploded")))


@dataclass(frozen=True)
class _Verdicted:
    """Minimal `HasVerdict` structural match: identity plus a verdict."""

    ref: HelmReleaseRef
    verdict: Verdict


def test_nothing_matched_reports_the_time_since_the_callers_start() -> None:
    # `start` precedes listing and matching, which `run()` can't make take time.
    elapsed_seen: list[float] = []
    events = EventLog()

    def no_match(elapsed: float) -> _Verdicted:
        elapsed_seen.append(elapsed)
        return _Verdicted(NO_MATCH_REF, Verdict.NO_MATCH)

    result = run_matched(
        [],
        start=4.0,
        clock=lambda: 10.0,
        total_deadline=1_000.0,
        concurrency=1,
        telemetry=PromotionTelemetry(
            writer=EventWriter(source="chart-manager", store=lambda: events), chart_name="loki", version="0.2.0", environment="dev"
        ),
        stage=Stage.ROLLOUT,
        success=Verdict.READY,
        no_match=no_match,
        work=lambda _status, _cancel: _Verdicted(NO_MATCH_REF, Verdict.READY),
        crash_label="test watcher",
        log_label="monitor",
        chart_name="loki",
        version="0.2.0",
        namespace=None,
    )

    assert elapsed_seen == [6.0]
    assert result.total_duration_seconds == 6.0
    assert events.events == []  # nothing matched opens no interval
