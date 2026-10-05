"""How `run_fanout` classifies a worker that raised, and how `run_matched` reports.

The interesting axis is not the happy path -- `MonitorService` and
`TestService` cover that end to end -- but the boundary between "a release
failed", "this run's infrastructure failed", and "the operator pressed
Ctrl-C". The third used to be indistinguishable from the second: it was
caught as a `BaseException` and reborn as `ChartManagerError`, so both
callers' `except Exception:` telemetry handlers put a network write in front
of the exit and the process returned 1 instead of 130.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from chart_manager.commands.promote.fanout import (
    RunResult,
    run_fanout,
    run_matched,
    sorted_by_ref,
)
from chart_manager.commands.promote.state import NO_MATCH_REF, Stage, Verdict
from chart_manager.integrations.helmrelease import HelmReleaseRef, HelmReleaseStatus
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError


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


def test_keyboard_interrupt_propagates_unwrapped_and_cancels_peers() -> None:
    """Ctrl-C must stay Ctrl-C all the way out of the fan-out.

    Wrapping it in `ChartManagerError` made it an `Exception`, which both
    services catch to close their telemetry interval -- a network write
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


def test_sorted_by_ref_orders_by_namespace_then_name() -> None:
    unsorted = [
        _Outcome(_ref("zeta", "a")),
        _Outcome(_ref("alpha", "b")),
        _Outcome(_ref("alpha", "a")),
    ]
    assert [(o.ref.namespace, o.ref.name) for o in sorted_by_ref(unsorted)] == [
        ("a", "alpha"),
        ("a", "zeta"),
        ("b", "alpha"),
    ]


# ----- run_matched: the telemetry bracket around the fan-out ----------------
#
# `MonitorService` and `TestService` both delegate here, so these cases are
# the single place the no-match, crash and Ctrl-C contracts are pinned for
# both stages. The service suites (and tests/commands/promote/test_telemetry.py)
# cover the same paths end to end through each service.


@dataclass(frozen=True)
class _Verdicted:
    """Minimal `HasVerdict` structural match: identity plus a verdict."""

    ref: HelmReleaseRef
    verdict: Verdict


@dataclass
class _RecordingTelemetry:
    """`PromotionTelemetry` stand-in that records the bracket it is handed."""

    calls: list[tuple[object, ...]] = field(default_factory=list)

    def started(self, stage: Stage, *, matched: int) -> None:
        self.calls.append(("started", stage, matched))

    def finished(self, stage: Stage, verdict: Verdict, *, total: int, failures: int) -> None:
        self.calls.append(("finished", stage, verdict, total, failures))


def _passing(status: HelmReleaseStatus, _cancel: threading.Event) -> _Verdicted:
    return _Verdicted(status.ref, Verdict.READY)


def _matched(
    statuses: list[HelmReleaseStatus],
    telemetry: _RecordingTelemetry,
    *,
    work: Callable[[HelmReleaseStatus, threading.Event], _Verdicted] = _passing,
    concurrency: int = 1,
    clock: Callable[[], float] = lambda: 10.0,
    **overrides: object,
) -> RunResult[_Verdicted]:
    """Call `run_matched` for the rollout stage with inert defaults."""
    kwargs: dict[str, object] = {
        "start": 4.0,
        "clock": clock,
        "total_deadline": 1_000.0,
        "concurrency": concurrency,
        "telemetry": telemetry,
        "stage": Stage.ROLLOUT,
        "success": Verdict.READY,
        "no_match": lambda elapsed: _Verdicted(NO_MATCH_REF, Verdict.NO_MATCH),
        "work": work,
        "crash_label": "test watcher",
        "log_label": "monitor",
        "chart_name": "loki",
        "version": "0.2.0",
        "namespace": None,
    }
    kwargs.update(overrides)
    return run_matched(statuses, **kwargs)  # type: ignore[arg-type]


def test_run_matched_no_match_is_one_synthetic_outcome_and_no_telemetry() -> None:
    # No interval was opened, so there is nothing to close: a started/finished
    # pair here would put a phantom endpoint on the promotion timeline.
    telemetry = _RecordingTelemetry()
    elapsed_seen: list[float] = []

    def no_match(elapsed: float) -> _Verdicted:
        elapsed_seen.append(elapsed)
        return _Verdicted(NO_MATCH_REF, Verdict.NO_MATCH)

    def never(_status: HelmReleaseStatus, _cancel: threading.Event) -> _Verdicted:
        raise AssertionError("work must not run when nothing matched")

    result = _matched([], telemetry, work=never, no_match=no_match)

    assert result == RunResult(
        outcomes=(_Verdicted(NO_MATCH_REF, Verdict.NO_MATCH),),
        total_duration_seconds=6.0,
        total_timed_out=False,
    )
    assert elapsed_seen == [6.0]  # clock() - start, handed to the factory
    assert result.ok is False
    assert telemetry.calls == []


def test_run_matched_brackets_a_clean_run_and_sorts_outcomes_by_ref() -> None:
    telemetry = _RecordingTelemetry()
    statuses = [
        _status(_ref("zeta", "a")),
        _status(_ref("alpha", "b")),
        _status(_ref("alpha", "a")),
    ]

    result = _matched(statuses, telemetry, concurrency=3)

    # Completion order is thread-scheduling noise; the result must not be.
    assert [(o.ref.namespace, o.ref.name) for o in result.outcomes] == [
        ("a", "alpha"),
        ("a", "zeta"),
        ("b", "alpha"),
    ]
    assert result.ok is True
    assert result.total_duration_seconds == 6.0
    assert result.total_timed_out is False
    assert telemetry.calls == [
        ("started", Stage.ROLLOUT, 3),
        ("finished", Stage.ROLLOUT, Verdict.READY, 3, 0),
    ]


def test_run_matched_closes_with_the_folded_verdict_and_failure_count() -> None:
    telemetry = _RecordingTelemetry()

    def work(status: HelmReleaseStatus, _cancel: threading.Event) -> _Verdicted:
        verdict = Verdict.FAILED if status.ref.name == "bad" else Verdict.PASSED
        return _Verdicted(status.ref, verdict)

    result = _matched(
        [_status(_ref("good")), _status(_ref("bad"))],
        telemetry,
        work=work,
        stage=Stage.HELM_TEST,
        success=Verdict.PASSED,
    )

    assert [o.ref.name for o in result.failures] == ["bad"]
    assert telemetry.calls[-1] == ("finished", Stage.HELM_TEST, Verdict.FAILED, 2, 1)


def test_run_matched_closes_the_interval_as_failed_when_a_worker_crashes() -> None:
    # One release reports, the next raises: the interval must still close,
    # and the release that never reported counts as a failure, not as zero.
    telemetry = _RecordingTelemetry()
    # `run_fanout` reads the clock right after recording an outcome, so the
    # crashing worker waits for that read: exactly one outcome is recorded
    # before the crash, whatever order the threads are scheduled in.
    recorded = threading.Event()

    def clock() -> float:
        recorded.set()
        return 10.0

    def work(status: HelmReleaseStatus, _cancel: threading.Event) -> _Verdicted:
        if status.ref.name == "crashes":
            assert recorded.wait(timeout=5)
            raise ChartManagerError("kubectl exploded")
        return _Verdicted(status.ref, Verdict.READY)

    with pytest.raises(ChartManagerError, match="kubectl exploded"):
        _matched(
            [_status(_ref("reports")), _status(_ref("crashes"))],
            telemetry,
            work=work,
            clock=clock,
            concurrency=2,
        )

    assert telemetry.calls == [
        ("started", Stage.ROLLOUT, 2),
        ("finished", Stage.ROLLOUT, Verdict.FAILED, 2, 1),
    ]


def test_run_matched_does_not_close_the_interval_on_keyboard_interrupt() -> None:
    """Ctrl-C must not wait on a telemetry write.

    The crash handler catches `Exception`, not `BaseException`: closing the
    interval is a network write, and an interrupted run has no terminal
    state to report anyway.
    """
    telemetry = _RecordingTelemetry()

    def work(_status: HelmReleaseStatus, _cancel: threading.Event) -> _Verdicted:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _matched([_status(_ref("loki"))], telemetry, work=work)

    assert telemetry.calls == [("started", Stage.ROLLOUT, 1)]


def test_run_matched_reports_a_cancel_as_total_timed_out() -> None:
    # The cancel flag `work` receives is the one the fan-out sets, and the
    # result reports whether it was set. "second" blocks on that flag, so it
    # only returns once "first" has tripped `cancel_on`.
    telemetry = _RecordingTelemetry()

    def work(status: HelmReleaseStatus, cancel: threading.Event) -> _Verdicted:
        if status.ref.name == "first":
            return _Verdicted(status.ref, Verdict.FAILED)
        assert cancel.wait(timeout=5)
        return _Verdicted(status.ref, Verdict.TIMED_OUT)

    result = _matched(
        [_status(_ref("first")), _status(_ref("second"))],
        telemetry,
        work=work,
        concurrency=2,
        cancel_on=lambda o: o.verdict is Verdict.FAILED,
    )

    assert [o.verdict for o in result.outcomes] == [Verdict.FAILED, Verdict.TIMED_OUT]
    assert result.total_timed_out is True


def test_run_matched_reports_an_exhausted_deadline_as_total_timed_out() -> None:
    telemetry = _RecordingTelemetry()
    result = _matched([_status(_ref("loki"))], telemetry, clock=lambda: 2_000.0)
    assert result.total_timed_out is True
