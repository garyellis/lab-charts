"""Promotion events from `monitor.run` and `test.run`, and the phase tables behind them."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from chart_manager.commands.promote import monitor, test
from chart_manager.commands.promote.monitor import MonitorRequest
from chart_manager.commands.promote.state import (
    PROMOTE_PHASE,
    TERMINAL_PHASES,
    PromoteStatus,
    Stage,
    Verdict,
    run_verdict,
)
from chart_manager.commands.promote.telemetry import emit_promotion
from chart_manager.commands.promote.test import TestRequest
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.settings import Settings
from chart_manager.shared.events.failure import emit_non_fatal
from chart_manager.shared.events.model import PromotionPhase
from chart_manager.shared.events.writer import EventWriter
from tests.commands.promote.conftest import (
    CHART,
    VERSION,
    EventLog,
    cluster,
    condition,
    helmrelease,
)
from tests.conftest import FakeCommandRunner, Reply, argv_prefix

ENV = "dev"
FAILED = (condition("Ready", "False", "InstallFailed"),)


def _monitor(runner: FakeCommandRunner, events: EventLog, **request: Any) -> monitor.MonitorResult:
    return monitor.run(
        MonitorRequest(**{"chart_name": CHART, "version": VERSION, "environment": ENV, **request}),
        runner=runner,
        settings=Settings(),
        events=EventWriter(source="chart-manager", store=lambda: events),
        sleep=lambda _s: None,
        clock=lambda: 0.0,
        rand=lambda _lo, _hi: 0.0,
    )


def _test(runner: FakeCommandRunner, events: EventLog, **request: Any) -> test.TestResult:
    fields = {"chart_name": CHART, "version": VERSION, "environment": ENV, **request}
    return test.run(
        TestRequest(per_hr_timeout_seconds=60.0, total_timeout_seconds=300.0, **fields),
        runner=runner,
        settings=Settings(),
        events=EventWriter(source="chart-manager", store=lambda: events),
        clock=lambda: 0.0,
    )


def test_monitor_brackets_a_converged_rollout() -> None:
    events = EventLog()

    assert _monitor(cluster(helmrelease()), events).ok is True

    assert events.phases == [PromotionPhase.WAITING_ROLLOUT, PromotionPhase.ROLLOUT_OK]
    opened, closed = events.events
    assert (opened.chart_name, opened.chart_version, opened.environment) == (CHART, VERSION, ENV)
    assert opened.detail == {"stage": "rollout", "matched": 1}
    assert closed.detail == {"stage": "rollout", "verdict": "ready", "total": 1, "failures": 0}


def _failing_rollout() -> FakeCommandRunner:
    return cluster(helmrelease("good", "ns"), helmrelease("bad", "ns", conditions=FAILED))


def _failing_helm_test() -> FakeCommandRunner:
    runner = cluster(helmrelease("good", "ns"), helmrelease("bad", "ns"))
    return runner.respond(argv_prefix("helm", "test", "bad"), returncode=1, stderr="pod failed")


@pytest.mark.parametrize(
    ("stage", "runner", "name", "phases"),
    [
        (_monitor, _failing_rollout, "rollout",
         [PromotionPhase.WAITING_ROLLOUT, PromotionPhase.ABANDONED]),
        (_test, _failing_helm_test, "helm-test",
         [PromotionPhase.HELM_TEST_RUN, PromotionPhase.HELM_TEST_FAILED]),
    ],
    ids=["rollout", "helm-test"],
)  # fmt: skip
def test_one_failure_among_two_releases_closes_the_run_as_failed(
    stage: Callable[..., Any],
    runner: Callable[[], FakeCommandRunner],
    name: str,
    phases: list[PromotionPhase],
) -> None:
    events = EventLog()

    result = stage(runner(), events)

    assert [o.ref.name for o in result.failures] == ["bad"]
    assert events.phases == phases
    opened, closing = events.events
    assert opened.detail == {"stage": name, "matched": 2}
    assert closing.detail == {"stage": name, "verdict": "failed", "total": 2, "failures": 1}


def test_a_crashed_run_still_closes_and_counts_the_unreported_release_as_failed() -> None:
    events = EventLog()
    crash = Reply(raises=ChartManagerError("kubectl exploded"))
    runner = cluster(helmrelease("a", "ns"), [helmrelease("b", "ns", generation=2), crash])

    with pytest.raises(ChartManagerError, match="kubectl exploded"):
        _monitor(runner, events, concurrency=1)

    assert events.phases == [PromotionPhase.WAITING_ROLLOUT, PromotionPhase.ABANDONED]
    # `failures: 1` relies on fan-out submitting in order under concurrency=1: "a" reports first.
    assert events.events[1].detail == {
        "stage": "rollout", "verdict": "failed", "total": 2, "failures": 1,
    }  # fmt: skip


def test_ctrl_c_leaves_the_interval_open() -> None:
    # Closing it is a network write standing between Ctrl-C and the exit.
    events = EventLog()
    runner = cluster([helmrelease(generation=2), Reply(raises=KeyboardInterrupt())])

    with pytest.raises(KeyboardInterrupt):
        _monitor(runner, events)

    assert events.phases == [PromotionPhase.WAITING_ROLLOUT]


def test_helm_test_pass_also_reports_promoted() -> None:
    # A green helm test verifies the promotion live, so it closes the promotion too.
    events = EventLog()

    assert _test(cluster(helmrelease()), events).ok is True

    assert events.phases == [
        PromotionPhase.HELM_TEST_RUN,
        PromotionPhase.HELM_TEST_OK,
        PromotionPhase.PROMOTED,
    ]
    assert events.events[0].detail == {"stage": "helm-test", "matched": 1}


def test_helm_test_failure_closes_with_helm_test_failed_and_no_promoted() -> None:
    events = EventLog()
    runner = cluster(helmrelease())
    runner.respond(argv_prefix("helm", "test"), returncode=1, stderr="pod failed")

    assert _test(runner, events).ok is False

    assert events.phases == [PromotionPhase.HELM_TEST_RUN, PromotionPhase.HELM_TEST_FAILED]


@pytest.mark.parametrize(
    ("stage", "opened"),
    [(_monitor, PromotionPhase.WAITING_ROLLOUT), (_test, PromotionPhase.HELM_TEST_RUN)],
)
def test_an_all_suspended_run_opens_the_interval_but_never_closes_it(
    stage: Callable[..., Any], opened: PromotionPhase
) -> None:
    # Nothing was watched or tested, so nothing is ROLLOUT_OK or PROMOTED.
    events = EventLog()

    result = stage(cluster(helmrelease(suspend=True)), events)

    assert [o.verdict for o in result.outcomes] == [Verdict.SKIPPED_SUSPENDED]
    assert events.phases == [opened]


@pytest.mark.parametrize("stage", [_monitor, _test])
@pytest.mark.parametrize("request_", [{"environment": None}, {"version": "9.9.9"}])
def test_an_ad_hoc_or_unmatched_run_emits_nothing(
    stage: Callable[..., Any], request_: dict[str, Any]
) -> None:
    events = EventLog()
    stage(cluster(helmrelease()), events, **request_)
    assert events.events == []


def test_a_failed_event_write_does_not_fail_the_run(caplog: pytest.LogCaptureFixture) -> None:
    events = EventLog(raises=KeyError("COSMOS_ENDPOINT"))

    assert _monitor(cluster(helmrelease()), events).ok is True

    assert len(events.events) == 2  # both attempted, both swallowed
    assert "non-fatal" in caplog.text


def test_the_swallow_records_the_exception_type_not_only_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Transport errors often stringify to "", so the log carries the type too."""

    def emit() -> None:
        raise KeyError("COSMOS_ENDPOINT")

    with caplog.at_level("WARNING"):
        error = emit_non_fatal(emit, strict=False, what="promotion")

    assert isinstance(error, KeyError)
    [record] = [r for r in caplog.records if "non-fatal" in r.message]
    assert record.levelname == "WARNING"
    assert "promotion event emission failed (non-fatal)" in record.getMessage()
    assert "KeyError" in record.getMessage()


def test_emit_promotion_logs_a_failed_write_under_its_label(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events = EventLog(raises=KeyError("COSMOS_ENDPOINT"))
    writer = EventWriter(source="chart-manager", store=lambda: events)

    with caplog.at_level("WARNING"):
        emit_promotion(
            writer,
            chart_name=CHART,
            chart_version=VERSION,
            environment=ENV,
            phase=PromotionPhase.ROLLOUT_OK,
            what="promotion rollout_complete",
        )

    assert events.phases == [PromotionPhase.ROLLOUT_OK]
    assert "promotion rollout_complete event emission failed (non-fatal)" in caplog.text


def test_every_terminal_phase_pair_is_reachable_from_a_run_verdict() -> None:
    reachable = {
        (Stage.ROLLOUT, run_verdict([v], success=Verdict.READY)) for v in Verdict
    } | {(Stage.HELM_TEST, run_verdict([v], success=Verdict.PASSED)) for v in Verdict}
    assert set(TERMINAL_PHASES) <= reachable


def test_a_skip_alongside_a_real_pass_still_reports_the_pass() -> None:
    verdict = run_verdict([Verdict.SKIPPED_SUSPENDED, Verdict.PASSED], success=Verdict.PASSED)
    assert verdict is Verdict.PASSED


def test_promoted_is_only_reported_after_a_green_helm_test() -> None:
    emitting_promoted = {
        key for key, phases in TERMINAL_PHASES.items() if PromotionPhase.PROMOTED in phases
    }
    assert emitting_promoted == {(Stage.HELM_TEST, Verdict.PASSED)}


def test_no_promote_status_is_missing_a_phase_decision() -> None:
    assert set(PROMOTE_PHASE) == set(PromoteStatus)
