"""`monitor.run()`: poll each matched HelmRelease until it converges, fails or runs out of time."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

import pytest

from chart_manager.commands.promote import MonitorRequest, MonitorResult, Transition
from chart_manager.commands.promote.monitor import run
from chart_manager.integrations.helmrelease import HelmReleaseRef
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.events.writer import EventWriter
from chart_manager.shared.settings import Settings
from tests.commands.promote.conftest import (
    CHART,
    HR,
    VERSION,
    Clock,
    EventLog,
    calls,
    cluster,
    condition,
    deployment,
    failure,
    helmrelease,
    items,
    workloads,
)
from tests.conftest import FakeCommandRunner, argv_prefix

PROGRESSING = (condition("Ready", "Unknown", "Progressing"),)
INSTALL_FAILED = (condition("Ready", "False", "InstallFailed", "bad"),)


def _monitor(
    runner: FakeCommandRunner,
    *,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] = lambda _s: None,
    rand: Callable[[float, float], float] = lambda _lo, _hi: 0.0,
    progress: Callable[[HelmReleaseRef, Transition], None] | None = None,
    **request: Any,
) -> MonitorResult:
    return run(
        MonitorRequest(**{"chart_name": CHART, "version": VERSION, "concurrency": 2, **request}),
        runner=runner,
        settings=Settings(kube_context="lab", command_timeout=30.0),
        events=EventWriter(EventLog()),
        sleep=sleep,
        clock=clock or Clock(),
        rand=rand,
        progress=progress,
    )


def _status_reads(runner: FakeCommandRunner, name: str = "loki") -> int:
    return sum(1 for argv in calls(runner, "kubectl", "-n") if argv[3:6] == ("get", HR, name))


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"chart_name": ""}, "chart_name"),
        ({"concurrency": 0}, "concurrency"),
        ({"per_hr_timeout_seconds": 1.0, "total_timeout_seconds": 2.0}, "poll interval"),
        ({"per_hr_timeout_seconds": 600.0, "total_timeout_seconds": 60.0}, "total_timeout"),
        ({"per_poll_timeout_seconds": float("nan")}, "per_poll_timeout_seconds"),
        ({"per_hr_timeout_seconds": float("nan")}, "per_hr_timeout_seconds"),
        ({"total_timeout_seconds": float("nan")}, "total_timeout_seconds"),
        ({"per_hr_timeout_seconds": "5m"}, "per_hr_timeout_seconds must be a number"),
    ],
)
def test_the_request_rejects_bad_bounds(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ChartManagerError, match=message):
        MonitorRequest(**{"chart_name": CHART, "version": VERSION, **fields})


def test_a_per_hr_budget_of_one_poll_interval_is_allowed() -> None:
    request = MonitorRequest(CHART, VERSION, per_hr_timeout_seconds=3.0, total_timeout_seconds=3.0)
    assert request.per_hr_timeout_seconds == 3.0


def test_a_failed_listing_raises() -> None:
    runner = FakeCommandRunner().respond(argv_prefix("kubectl", "get", HR), returncode=1)
    with pytest.raises(ExternalCommandError):
        _monitor(runner)


def test_nothing_matched_is_one_no_match_outcome() -> None:
    runner = cluster(
        helmrelease("a", "ns1", chart="other"), helmrelease("b", "ns2", version="9.9.9")
    )

    result = _monitor(runner)

    [outcome] = result.outcomes
    assert (outcome.verdict, outcome.reason) == ("no-match", "NoHelmReleasesMatched")
    assert result.ok is False


def test_a_converged_release_is_ready_without_another_poll() -> None:
    runner = cluster(helmrelease())
    runner.respond_each(workloads(), items(deployment()))

    [outcome] = _monitor(runner).outcomes

    assert outcome.verdict == "ready"
    assert outcome.diagnostics is None
    assert _status_reads(runner) == 1
    assert calls(runner, "kubectl", "get", "events") == []


def test_lagging_releases_are_polled_until_ready() -> None:
    runner = cluster(
        [
            helmrelease(generation=2, observed=1, history="0.1.0"),
            helmrelease(generation=2, observed=2, history="0.1.0"),
            helmrelease(generation=2, observed=2),
        ]
    )
    runner.respond_each(workloads(), items(deployment(converged=False)), items(deployment()))
    slept: list[float] = []
    jitter: list[tuple[float, float]] = []
    seen: list[str] = []

    def rand(lo: float, hi: float) -> float:
        jitter.append((lo, hi))
        return 1.25

    [outcome] = _monitor(
        runner, sleep=slept.append, rand=rand, progress=lambda _r, t: seen.append(t.phase)
    ).outcomes

    assert outcome.verdict == "ready"
    phases = [t.phase for t in outcome.recent_transitions]
    assert phases[:2] == ["GenerationLag", "HistoryLag"]
    assert phases[-1] == "Ready"
    assert seen == phases
    assert _status_reads(runner) == 4
    assert len(calls(runner, "kubectl", "get", "deployment,statefulset,daemonset")) == 2
    # One jittered start inside the poll interval, then the fixed 3s interval.
    assert jitter == [(0.0, 3.0)]
    assert slept == [1.25, 3.0, 3.0, 3.0]


def test_a_terminal_failure_ends_the_watch_and_reports_why() -> None:
    runner = cluster([helmrelease(conditions=INSTALL_FAILED), helmrelease()])

    [outcome] = _monitor(runner).outcomes

    assert (outcome.verdict, outcome.reason) == ("failed", "InstallFailed")
    assert _status_reads(runner) == 1
    assert outcome.diagnostics is not None
    assert "## loki/loki - failed: InstallFailed" in outcome.diagnostics
    assert "### Status" in outcome.diagnostics
    assert "### Events (namespace loki)" in outcome.diagnostics


def test_diagnostics_read_events_where_the_workloads_run() -> None:
    runner = cluster(
        helmrelease(
            namespace="cluster-system", target_namespace="observability", conditions=INSTALL_FAILED
        )
    )

    [outcome] = _monitor(runner).outcomes

    assert outcome.diagnostics is not None
    assert "### Events (namespace observability)" in outcome.diagnostics
    assert [argv[4] for argv in calls(runner, "kubectl", "get", "events")] == ["observability"]


def test_a_workload_that_never_converges_times_out_the_release() -> None:
    runner = cluster(helmrelease())
    runner.respond_each(workloads(), items(deployment(converged=False)))
    runner.respond(argv_prefix("kubectl", "get", "events"), stdout="BackOff pulling image")
    # Hold the clock until the first poll is recorded, then pass per-HR but not total.
    clock = Clock(step=400.0, warmup=5)

    [outcome] = _monitor(runner, clock=clock, per_poll_timeout_seconds=7.0).outcomes

    assert (outcome.verdict, outcome.reason) == ("timed-out", "PerHRBudgetExhausted")
    assert outcome.diagnostics is not None
    assert "#### Deployment/loki/loki-app\nBackOff pulling image" in outcome.diagnostics
    # Every cluster read is pinned to the context and bounded by the per-poll timeout.
    assert {r.timeout for r in runner.records} == {7.0}
    assert all(r.args[-2:] == ("--context", "lab") for r in runner.records)


def test_the_total_budget_times_out_the_releases_still_waiting() -> None:
    runner = cluster(
        helmrelease("a0", "ns"),
        helmrelease("a1", "ns", generation=2, conditions=PROGRESSING),
        helmrelease("a2", "ns", generation=2, conditions=PROGRESSING),
    )

    result = _monitor(runner, clock=Clock(step=500.0), concurrency=3)

    assert sorted(o.verdict for o in result.outcomes) == ["ready", "timed-out", "timed-out"]


def test_a_suspended_release_is_skipped_without_polling() -> None:
    runner = cluster(helmrelease(suspend=True))

    result = _monitor(runner)

    assert [o.verdict for o in result.outcomes] == ["skipped-suspended"]
    assert result.ok is True
    assert _status_reads(runner) == 1
    assert calls(runner, "kubectl", "get", "deployment,statefulset,daemonset") == []
    assert calls(runner, "kubectl", "get", "events") == []


def test_repeated_polls_record_one_transition_per_change() -> None:
    gen_lag = helmrelease(generation=2, conditions=PROGRESSING)
    history_lag = helmrelease(generation=2, observed=2, history="old", conditions=PROGRESSING)
    runner = cluster([gen_lag] * 10 + [history_lag] * 2)

    [outcome] = _monitor(runner, clock=Clock(step=400.0, warmup=50)).outcomes

    phases = [t.phase for t in outcome.recent_transitions]
    assert phases == ["GenerationLag", "HistoryLag"]
    assert all(a != b for a, b in itertools.pairwise(phases))


def test_only_the_last_five_transitions_are_kept() -> None:
    gen_lag = helmrelease(generation=2, conditions=PROGRESSING)
    history_lag = helmrelease(generation=2, observed=2, history="old", conditions=PROGRESSING)
    runner = cluster([gen_lag, history_lag] * 6)

    [outcome] = _monitor(runner, clock=Clock(step=400.0, warmup=50)).outcomes

    assert len(outcome.recent_transitions) == 5


def test_a_release_deleted_mid_watch_fails_as_disappeared() -> None:
    not_found = 'Error from server (NotFound): helmreleases "loki" not found'
    runner = cluster([helmrelease(generation=2, conditions=PROGRESSING), failure(not_found)])

    [outcome] = _monitor(runner).outcomes

    assert (outcome.verdict, outcome.reason) == ("failed", "Disappeared")


def test_a_flaky_poll_is_recorded_and_the_watch_continues() -> None:
    runner = cluster([helmrelease(generation=2, conditions=PROGRESSING), failure("flake")])

    [outcome] = _monitor(runner, clock=Clock(step=200.0)).outcomes

    assert outcome.verdict == "timed-out"
    assert "PollError" in [t.phase for t in outcome.recent_transitions]


def test_a_raising_progress_callback_does_not_break_the_watch() -> None:
    runner = cluster(helmrelease())

    def explode(_ref: HelmReleaseRef, _t: Transition) -> None:
        raise RuntimeError("callback crash")

    assert [o.verdict for o in _monitor(runner, progress=explode).outcomes] == ["ready"]


@pytest.mark.parametrize(("fail_fast", "peer"), [(True, "timed-out"), (False, "ready")])
def test_fail_fast_cancels_the_peers_of_a_failed_release(fail_fast: bool, peer: str) -> None:
    runner = cluster(helmrelease("aaa", "ns", conditions=INSTALL_FAILED), helmrelease("zzz", "ns"))

    result = _monitor(runner, concurrency=1, fail_fast=fail_fast)

    by_name = {o.ref.name: o for o in result.outcomes}
    assert by_name["aaa"].verdict == "failed"
    assert by_name["zzz"].verdict == peer
    if fail_fast:
        assert by_name["zzz"].reason == "TotalBudgetExhausted"
