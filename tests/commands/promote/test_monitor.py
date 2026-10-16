"""`monitor.run()`: poll each matched HelmRelease until it converges, fails or runs out of time."""

from __future__ import annotations

from typing import Any

import pytest

from chart_manager.commands.promote.state import DETAIL_MAX, Reason
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.progress import ProgressEvent, RowUpdate
from tests.commands.promote.conftest import (
    HR,
    Clock,
    calls,
    cluster,
    condition,
    deployment,
    failure,
    helmrelease,
    items,
    run_monitor,
    workloads,
)
from tests.conftest import FakeCommandRunner, Reply, argv_prefix

PROGRESSING = (condition("Ready", "Unknown", "Progressing"),)
INSTALL_FAILED = (condition("Ready", "False", "InstallFailed", "bad"),)


def _status_reads(runner: FakeCommandRunner, name: str = "loki") -> int:
    return sum(1 for argv in calls(runner, "kubectl", "-n") if argv[3:6] == ("get", HR, name))


def test_a_failed_listing_raises() -> None:
    runner = FakeCommandRunner().respond(argv_prefix("kubectl", "get", HR), returncode=1)
    with pytest.raises(ExternalCommandError):
        run_monitor(runner)


def test_a_converged_release_is_ready_without_another_poll() -> None:
    runner = cluster(helmrelease())
    runner.respond_each(workloads(), items(deployment()))

    [outcome] = run_monitor(runner).outcomes

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
    clock = Clock()
    slept: list[float] = []
    jitter: list[tuple[float, float]] = []
    seen: list[ProgressEvent | RowUpdate] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.sleep(seconds)

    def rand(lo: float, hi: float) -> float:
        jitter.append((lo, hi))
        return 1.25

    result = run_monitor(runner, clock=clock, sleep=sleep, rand=rand, progress=seen.append)

    [outcome] = result.outcomes

    assert outcome.verdict == "ready"
    phases = [t.phase for t in outcome.transitions]
    assert phases[:2] == ["GenerationLag", "HistoryLag"]
    assert phases[-1] == "Ready"
    assert seen == [
        RowUpdate(("loki", "loki"), "phase", t.phase, t.detail)
        for t in outcome.transitions
    ]
    assert _status_reads(runner) == 4
    assert len(calls(runner, "kubectl", "get", "deployment,statefulset,daemonset")) == 2
    # One jittered start inside the poll interval, then the fixed 3s interval.
    assert jitter == [(0.0, 3.0)]
    assert slept == [1.25, 3.0, 3.0, 3.0]
    assert outcome.duration_seconds == result.total_duration_seconds == 10.25


READY = condition("Ready", "True", "ReconciliationSucceeded")
RELEASED = condition("Released", "True", "UpgradeSucceeded")
STALLED = condition("Stalled", "True", message="stuck")
WAITED = ("timed-out", "PerHRBudgetExhausted")


@pytest.mark.parametrize(
    ("release", "listed", "outcome", "phase", "detail"),
    [
        (helmrelease(suspend=True), (), ("skipped-suspended", "Suspended"), "Suspended",
         "HR spec.suspend=true"),
        # Suspension outranks the Stalled it was suspended because of.
        (helmrelease(suspend=True, conditions=[STALLED]), (),
         ("skipped-suspended", "Suspended"), "Suspended", "HR spec.suspend=true"),
        (helmrelease(conditions=[STALLED]), (), ("failed", "Stalled"), "Stalled", "stuck"),
        (helmrelease(conditions=[condition("Stalled", "True", message="x" * 500)]), (),
         ("failed", "Stalled"), "Stalled", "x" * DETAIL_MAX),
        # Flux's own reason passes through untranslated.
        *[
            (helmrelease(conditions=[condition("Ready", "False", reason, "bad")]), (),
             ("failed", reason), f"Ready=False:{reason}", "bad")
            for reason in ("InstallFailed", "UpgradeFailed", "ReconciliationFailed",
                           "ArtifactFailed", "RetryExhausted")
        ],
        (helmrelease(conditions=[READY, RELEASED,
                                 condition("TestSuccess", "False", "TestFailed", "probe died")]),
         (), ("failed", "TestFailed"), "TestSuccess=False", "probe died"),
        (helmrelease(conditions=[READY, RELEASED, condition("TestSuccess", "False")]), (),
         ("failed", "TestFailed"), "TestSuccess=False", ""),
        (helmrelease(conditions=[READY, RELEASED,
                                 condition("TestSuccess", "False", "SomethingFluxShippedLater")]),
         (), ("failed", "SomethingFluxShippedLater"), "TestSuccess=False", ""),
        # TestSuccess=False before Released=True is the hook's pre-run state.
        (helmrelease(conditions=[READY, condition("Released", "False"),
                                 condition("TestSuccess", "False", "TestFailed")]),
         (deployment(),), ("ready", "Ready"), "Ready",
         "HR Ready=True and all workloads converged"),
        (helmrelease(), (deployment(),), ("ready", "Ready"), "Ready",
         "HR Ready=True and all workloads converged"),
        # A release with no owned workloads reads as converged (discovery F12b).
        (helmrelease(), (), ("ready", "Ready"), "Ready",
         "HR Ready=True and all workloads converged"),
        (helmrelease(generation=2, conditions=[condition("Stalled", "False")]), (), WAITED,
         "GenerationLag", "obs-gen=1/2 history=0.2.0 requested=0.2.0"),
        # Flux retries out of a non-terminal Ready=False.
        (helmrelease(conditions=[condition("Ready", "False", "Progressing")]), (), WAITED,
         "WaitingForReady:Progressing",
         "obs-gen=1/1 history=0.2.0 requested=0.2.0 ready=False(Progressing)"),
        (helmrelease(conditions=[]), (), WAITED, "WaitingForReady",
         "obs-gen=1/1 history=0.2.0 requested=0.2.0"),
        (helmrelease(),
         (deployment("a", converged=False), deployment("b", converged=False), deployment("c")),
         WAITED, "WaitingForWorkloads:2",
         "obs-gen=1/1 history=0.2.0 requested=0.2.0 ready=True(ReconciliationSucceeded) "
         "pending=[Deployment/loki/a,Deployment/loki/b]"),
    ],
    ids=[
        "suspended", "suspended-outranks-stalled", "stalled", "stalled-detail-truncated",
        "install-failed", "upgrade-failed", "reconciliation-failed", "artifact-failed",
        "retry-exhausted", "test-failed", "test-failed-no-reason", "test-reason-unmodelled",
        "test-failed-before-released", "ready-workloads-converged", "ready-no-workloads",
        "generation-lag", "ready-false-progressing", "no-conditions", "workloads-pending",
    ],
)  # fmt: skip
def test_the_release_status_decides_the_verdict(
    release: dict[str, Any],
    listed: tuple[dict[str, Any], ...],
    outcome: tuple[str, str],
    phase: str,
    detail: str,
) -> None:
    runner = cluster(release)
    runner.respond_each(workloads(), items(*listed))

    [result] = run_monitor(runner, per_hr_timeout_seconds=3.0).outcomes

    assert (result.verdict, result.reason) == outcome
    # A reason the model names comes back as `Reason`; an unmodelled one as a plain string.
    assert isinstance(result.reason, Reason) is (outcome[1] in {r.value for r in Reason})
    first = result.transitions[0]
    assert (first.phase, first.detail) == (phase, detail)


def test_a_terminal_failure_ends_the_watch_and_reports_why() -> None:
    runner = cluster([helmrelease(conditions=INSTALL_FAILED), helmrelease()])

    [outcome] = run_monitor(runner).outcomes

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

    [outcome] = run_monitor(runner).outcomes

    assert outcome.diagnostics is not None
    assert "### Events (namespace observability)" in outcome.diagnostics
    assert [argv[4] for argv in calls(runner, "kubectl", "get", "events")] == ["observability"]


def test_a_workload_that_never_converges_times_out_the_release() -> None:
    runner = cluster(helmrelease())
    runner.respond_each(workloads(), items(deployment(converged=False)))
    runner.respond(argv_prefix("kubectl", "get", "events"), stdout="BackOff pulling image")

    [outcome] = run_monitor(runner, per_poll_timeout_seconds=7.0).outcomes

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

    result = run_monitor(
        runner, concurrency=3, per_hr_timeout_seconds=30.0, total_timeout_seconds=30.0
    )

    assert [(o.verdict, o.reason) for o in result.outcomes] == [
        ("ready", "Ready"),
        ("timed-out", "TotalBudgetExhausted"),
        ("timed-out", "TotalBudgetExhausted"),
    ]
    assert result.total_timed_out is True


def test_outcomes_are_sorted_by_namespace_then_name() -> None:
    runner = cluster(helmrelease("zeta", "a"), helmrelease("alpha", "b"), helmrelease("alpha", "a"))

    result = run_monitor(runner, concurrency=1)

    assert [(o.ref.namespace, o.ref.name) for o in result.outcomes] == [
        ("a", "alpha"),
        ("a", "zeta"),
        ("b", "alpha"),
    ]


def test_a_suspended_release_is_skipped_without_polling() -> None:
    runner = cluster(helmrelease(suspend=True))

    result = run_monitor(runner)

    assert [o.verdict for o in result.outcomes] == ["skipped-suspended"]
    assert result.ok is True
    assert _status_reads(runner) == 1
    assert calls(runner, "kubectl", "get", "deployment,statefulset,daemonset") == []
    assert calls(runner, "kubectl", "get", "events") == []


GEN_LAG = helmrelease(generation=2, conditions=PROGRESSING)
HISTORY_LAG = helmrelease(generation=2, observed=2, history="old", conditions=PROGRESSING)


def _pending(*names: str) -> Reply:
    return items(*(deployment(name, converged=False) for name in names))


@pytest.mark.parametrize(
    ("reads", "workload_reads", "phases"),
    [
        ([GEN_LAG] * 10 + [HISTORY_LAG] * 2, (), ["GenerationLag", "HistoryLag"]),
        (
            [helmrelease(generation=3, conditions=PROGRESSING),
             helmrelease(generation=3, observed=2, conditions=PROGRESSING)],
            (),
            ["GenerationLag", "GenerationLag"],
        ),
        (
            [helmrelease()],
            (_pending("a"), _pending("a", "b")),
            ["WaitingForWorkloads:1", "WaitingForWorkloads:2"],
        ),
        ([helmrelease()], (_pending("a", "b"), _pending("b", "a")), ["WaitingForWorkloads:2"]),
    ],
    ids=["same-phase-repeats", "observed-generation-moves", "pending-set-grows", "pending-reordered"],
)  # fmt: skip
def test_a_transition_is_recorded_only_when_the_situation_changes(
    reads: list[dict[str, Any]], workload_reads: tuple[Reply, ...], phases: list[str]
) -> None:
    runner = cluster(reads)
    if workload_reads:
        runner.respond_each(workloads(), *workload_reads)

    [outcome] = run_monitor(runner, per_hr_timeout_seconds=60.0).outcomes

    assert outcome.verdict == "timed-out"
    assert [t.phase for t in outcome.transitions] == [*phases, "TimedOut"]


def test_only_the_last_five_transitions_are_kept() -> None:
    runner = cluster([GEN_LAG, HISTORY_LAG] * 6)

    [outcome] = run_monitor(runner).outcomes

    assert len(outcome.transitions) == 5


def test_a_release_deleted_mid_watch_fails_as_disappeared() -> None:
    not_found = 'Error from server (NotFound): helmreleases "loki" not found'
    runner = cluster([helmrelease(generation=2, conditions=PROGRESSING), failure(not_found)])

    [outcome] = run_monitor(runner).outcomes

    assert (outcome.verdict, outcome.reason) == ("failed", "Disappeared")


def test_a_flaky_poll_is_recorded_and_the_watch_continues() -> None:
    runner = cluster([helmrelease(generation=2, conditions=PROGRESSING), failure("flake")])

    [outcome] = run_monitor(runner).outcomes

    assert outcome.verdict == "timed-out"
    assert "PollError" in [t.phase for t in outcome.transitions]


@pytest.mark.parametrize(("fail_fast", "peer"), [(True, "timed-out"), (False, "ready")])
def test_fail_fast_cancels_the_peers_of_a_failed_release(fail_fast: bool, peer: str) -> None:
    runner = cluster(helmrelease("aaa", "ns", conditions=INSTALL_FAILED), helmrelease("zzz", "ns"))

    result = run_monitor(runner, concurrency=1, fail_fast=fail_fast)

    by_name = {o.ref.name: o for o in result.outcomes}
    assert by_name["aaa"].verdict == "failed"
    assert by_name["zzz"].verdict == peer
    if fail_fast:
        assert by_name["zzz"].reason == "TotalBudgetExhausted"
    assert result.total_timed_out is fail_fast


@pytest.mark.parametrize(
    ("fail_fast", "phase", "reason"),
    [(False, "TimedOut", "PerHRBudgetExhausted"), (True, "Cancelled", "TotalBudgetExhausted")],
)
def test_a_watch_that_runs_out_of_time_reports_a_final_phase(
    fail_fast: bool, phase: str, reason: str
) -> None:
    runner = cluster(
        helmrelease("aaa", "ns", conditions=INSTALL_FAILED),
        helmrelease("zzz", "ns", generation=2, conditions=PROGRESSING),
    )
    seen: list[ProgressEvent | RowUpdate] = []

    run_monitor(runner, concurrency=1, fail_fast=fail_fast, progress=seen.append)

    assert [u for u in seen if isinstance(u, RowUpdate) and u.key == ("ns", "zzz")][-1] == (
        RowUpdate(("ns", "zzz"), "phase", phase, reason)
    )
