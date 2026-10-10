"""`test.run()`: reap stale test pods, run `helm test` per matched HelmRelease, report failures."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import ANY

import pytest

from chart_manager.commands.promote.test import TestRequest, TestResult, run
from chart_manager.plumbing.errors import ChartManagerError, CommandTimeout
from chart_manager.plumbing.progress import Progress, ProgressEvent, RowUpdate
from chart_manager.plumbing.text import truncate_bytes
from chart_manager.settings import Settings
from chart_manager.shared.events.writer import EventWriter
from tests.commands.promote.conftest import (
    CHART,
    HR,
    VERSION,
    Clock,
    calls,
    cluster,
    condition,
    failure,
    helmrelease,
    hook_pods,
    items,
    pod,
)
from tests.conftest import EventLog, FakeCommandRunner, Reply, argv_prefix, plain_argv

HELM_TEST = ("helm", "test", "loki", "--namespace", "loki")
TEST_FAILED = failure("Error: bare failure")


def _test(
    runner: FakeCommandRunner,
    *,
    clock: Callable[[], float] | None = None,
    progress: Progress = lambda _event: None,
    **request: Any,
) -> TestResult:
    fields = {"chart_name": CHART, "version": VERSION, "concurrency": 2, **request}
    fields.setdefault("per_hr_timeout_seconds", 60.0)
    fields.setdefault("total_timeout_seconds", 300.0)
    return run(
        TestRequest(**fields),
        runner=runner,
        settings=Settings(kube_context="lab"),
        events=EventWriter(source="chart-manager", store=lambda: EventLog()),
        clock=clock or Clock(),
        progress=progress,
    )


def _helm(runner: FakeCommandRunner, reply: Reply) -> FakeCommandRunner:
    runner.respond_each(argv_prefix("helm", "test"), reply)
    return runner


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"chart_name": ""}, "chart_name"),
        ({"version": ""}, "version"),
        ({"concurrency": 0}, "concurrency"),
        ({"pod_log_tail": 0}, "pod_log_tail"),
        ({"per_hr_timeout_seconds": 29.5}, "must be >= 30s"),
        ({"per_hr_timeout_seconds": 300.0, "total_timeout_seconds": 60.0}, "total_timeout"),
        ({"per_poll_timeout_seconds": float("nan")}, "per_poll_timeout_seconds"),
        ({"per_hr_timeout_seconds": float("nan")}, "per_hr_timeout_seconds"),
        ({"total_timeout_seconds": float("nan")}, "total_timeout_seconds"),
    ],
)
def test_the_request_rejects_bad_bounds(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ChartManagerError, match=message):
        TestRequest(**{"chart_name": CHART, "version": VERSION, **fields})


def test_a_per_hr_budget_of_thirty_seconds_is_allowed() -> None:
    request = TestRequest(CHART, VERSION, per_hr_timeout_seconds=30.0, total_timeout_seconds=30.0)
    assert request.per_hr_timeout_seconds == 30.0


def test_nothing_matched_is_one_no_match_outcome() -> None:
    runner = cluster(
        helmrelease("a", "ns1", chart="other"), helmrelease("b", "ns2", version="9.9.9")
    )

    result = _test(runner)

    [outcome] = result.outcomes
    assert (outcome.verdict, outcome.reason) == ("no-match", "NoHelmReleasesMatched")
    assert result.ok is False


def test_a_passing_release_runs_helm_test_once_and_reports_nothing_else() -> None:
    runner = cluster(helmrelease())

    [outcome] = _test(runner).outcomes

    assert (outcome.verdict, outcome.reason) == ("passed", "AllTestsPassed")
    assert outcome.diagnostics is None
    assert calls(runner, "helm") == [(*HELM_TEST, "--timeout", "60s", "--logs")]
    # No failure-path refresh, no events.
    assert len(calls(runner, "kubectl", "-n", "loki", "get", HR)) == 1
    assert calls(runner, "kubectl", "get", "events") == []


@pytest.mark.parametrize(
    ("release", "verdict", "reason"),
    [
        (helmrelease(suspend=True), "skipped-suspended", "Suspended"),
        (
            helmrelease(conditions=(condition("Ready", "Unknown", "Progressing"),)),
            "skipped-not-ready",
            "NotReleased",
        ),
        (helmrelease(generation=2), "skipped-not-ready", "GenerationLag"),
    ],
)
def test_an_unready_release_is_skipped_without_touching_it(
    release: dict[str, Any], verdict: str, reason: str
) -> None:
    runner = cluster(release)

    [outcome] = _test(runner).outcomes

    assert (outcome.verdict, outcome.reason) == (verdict, reason)
    assert calls(runner, "helm") == []
    assert calls(runner, "kubectl", "-n", "loki", "delete") == []
    assert calls(runner, "kubectl", "get", "events") == []


def test_finished_test_pods_are_deleted_before_helm_runs() -> None:
    runner = cluster(helmrelease())
    runner.respond_each(hook_pods, items(pod("loki-test-old", "Succeeded")), items())

    [outcome] = _test(runner).outcomes

    assert outcome.verdict == "passed"
    assert calls(runner, "kubectl", "-n", "loki", "delete") == [
        ("kubectl", "-n", "loki", "delete", "pod", "loki-test-old", "--ignore-not-found")
    ]
    assert len(calls(runner, "helm")) == 1


@pytest.mark.parametrize("phase", ["Running", "Unknown", ""])
def test_a_live_test_pod_refuses_the_run_and_deletes_nothing(phase: str) -> None:
    runner = cluster(helmrelease())
    runner.respond_each(
        hook_pods, items(pod("loki-test-old", "Succeeded"), pod("loki-test-live", phase))
    )

    [outcome] = _test(runner).outcomes

    assert (outcome.verdict, outcome.reason) == ("failed", "TestPodInFlight")
    assert "loki-test-live" in (outcome.diagnostics or "")
    assert calls(runner, "helm") == []
    assert calls(runner, "kubectl", "-n", "loki", "delete") == []


def test_a_test_pod_that_will_not_delete_fails_the_reap_and_says_why() -> None:
    runner = cluster(helmrelease())
    runner.respond_each(hook_pods, items(pod("old", "Succeeded"), pod("old2", "Failed")))
    delete = argv_prefix("kubectl", "-n", "loki", "delete", "pod", "old2")
    runner.respond(delete, returncode=1, stderr="forbidden")

    [outcome] = _test(runner).outcomes

    assert (outcome.verdict, outcome.reason) == ("failed", "ReapIncomplete")
    assert "loki/old2: forbidden" in (outcome.diagnostics or "")
    assert calls(runner, "helm") == []


@pytest.mark.parametrize(
    ("stderr", "verdict", "reason"),
    [
        ("Error: no tests to run for chart loki", "passed", "NoTestsDefined"),
        ("Error: pod loki-test already exists", "failed", "TestPodConflict"),
        ("Error: cluster unreachable", "failed", "HelmUnavailable"),
        ("Error: bare failure", "failed", "TestFailed"),
    ],
)
def test_helm_stderr_decides_the_verdict(stderr: str, verdict: str, reason: str) -> None:
    runner = _helm(cluster(helmrelease()), failure(stderr))

    [outcome] = _test(runner).outcomes

    assert (outcome.verdict, outcome.reason) == (verdict, reason)
    assert (outcome.diagnostics is None) is (verdict == "passed")


def test_a_test_failure_report_carries_pod_logs_and_the_post_run_status() -> None:
    after = helmrelease(
        conditions=(
            condition("Ready", "True"),
            condition("Released", "True"),
            condition("TestSuccess", "False", "TestFailed", "probe died"),
        )
    )
    runner = _helm(cluster([helmrelease(), after]), TEST_FAILED)
    runner.respond_each(hook_pods, items(), items(pod("loki-test", "Failed")))
    runner.respond(argv_prefix("kubectl", "-n", "loki", "logs", "loki-test"), stdout="boom")
    runner.respond(argv_prefix("kubectl", "get", "events", "-n", "loki"), stdout="ns event")

    [outcome] = _test(runner).outcomes

    assert outcome.reason == "TestFailed"
    assert outcome.last_status is not None and outcome.last_status.test_success is not None
    assert outcome.diagnostics is not None
    for text in ("probe died", "#### loki/loki-test (phase=Failed)\nboom", "ns event"):
        assert text in outcome.diagnostics
    assert [p.logs for p in outcome.test_pods] == ["boom"]


@pytest.mark.parametrize(("phase", "retried"), [("Failed", True), ("Running", False)])
def test_empty_logs_are_retried_with_previous_only_for_a_finished_pod(
    phase: str, retried: bool
) -> None:
    runner = _helm(cluster(helmrelease()), TEST_FAILED)
    runner.respond_each(hook_pods, items(), items(pod("loki-test", phase)))
    runner.respond(lambda argv: "--previous" in argv, stdout="previous boom")
    runner.respond(argv_prefix("kubectl", "-n", "loki", "logs"), stdout="")

    [outcome] = _test(runner).outcomes

    assert ("previous boom" in (outcome.diagnostics or "")) is retried
    previous = [a for a in calls(runner, "kubectl", "-n", "loki", "logs") if "--previous" in a]
    assert len(previous) == int(retried)


def test_a_swallowed_cluster_read_says_so_in_the_report_and_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = _helm(cluster([helmrelease(), failure("refresh boom")]), TEST_FAILED)
    runner.respond_each(hook_pods, items(), items(pod("loki-test", "Failed")))
    logs = argv_prefix("kubectl", "-n", "loki", "logs")
    runner.respond(logs, returncode=1, stderr="logs forbidden")

    with caplog.at_level("WARNING"):
        [outcome] = _test(runner).outcomes

    assert "status not refreshed: refresh boom" in (outcome.diagnostics or "")
    [snapshot] = outcome.test_pods
    assert snapshot.logs == "<logs unavailable: logs forbidden>"
    # A log read that failed is not a restarted container: no --previous retry.
    assert not [a for a in calls(runner, "kubectl", "-n", "loki", "logs") if "--previous" in a]
    [record] = [r for r in caplog.records if "test pod logs unavailable" in r.getMessage()]
    for text in ("logs forbidden", "pod=loki-test", "ns=loki", "release=loki"):
        assert text in record.getMessage()


def test_every_cluster_call_is_pinned_to_the_context_and_bounded() -> None:
    runner = _helm(cluster(helmrelease()), TEST_FAILED)
    runner.respond_each(hook_pods, items(), items(pod("loki-test", "Failed")))

    _test(runner, per_poll_timeout_seconds=7.0)

    kubectl = [r for r in runner.records if r.args[0] == "kubectl"]
    [helm] = [r for r in runner.records if r.args[0] == "helm"]
    assert {r.timeout for r in kubectl} == {7.0}
    assert all(r.args[-2:] == ("--context", "lab") for r in kubectl)
    assert helm.args[-2:] == ("--kube-context", "lab")


def test_unlistable_test_pods_are_distinguished_from_no_test_pods() -> None:
    runner = _helm(cluster(helmrelease()), TEST_FAILED)
    runner.respond_each(hook_pods, items(), failure("pods forbidden"))

    [outcome] = _test(runner).outcomes

    assert "<test pods unavailable: pods forbidden>" in (outcome.diagnostics or "")


def test_unreadable_events_do_not_break_the_report() -> None:
    timeout = CommandTimeout("command timed out")
    runner = cluster(helmrelease()).respond(argv_prefix("kubectl", "get", "events"), raises=timeout)
    _helm(runner, TEST_FAILED)

    [outcome] = _test(runner).outcomes

    assert outcome.reason == "TestFailed"
    assert "<events unavailable" in (outcome.diagnostics or "")


def test_a_helm_timeout_spends_the_per_hr_budget() -> None:
    timeout = CommandTimeout("command timed out")
    runner = cluster(helmrelease()).respond(argv_prefix("helm", "test"), raises=timeout)

    [outcome] = _test(runner).outcomes

    assert (outcome.verdict, outcome.reason) == ("timed-out", "PerHRBudgetExhausted")


def test_the_total_budget_stops_releases_before_helm_runs() -> None:
    runner = cluster(helmrelease())

    [outcome] = _test(runner, clock=Clock(step=200.0)).outcomes

    assert (outcome.verdict, outcome.reason) == ("timed-out", "TotalBudgetExhausted")
    assert calls(runner, "helm") == []


def test_the_total_budget_marks_the_run_timed_out() -> None:
    runner = cluster(*(helmrelease(f"a{i}", "ns") for i in range(3)))

    assert _test(runner, clock=Clock(step=250.0), concurrency=3).total_timed_out is True


@pytest.mark.parametrize(
    ("per_hr", "total", "late", "helm_timeout", "cap"),
    [
        (60.0, 300.0, False, "60s", 90.0),  # per-HR plus 30s of slack
        (60.0, 60.0, False, "60s", 60.0),  # bounded by the total budget
        (45.5, 300.0, False, "45.5s", 75.5),
        (60.0, 300.0, True, "60s", 50.0),  # 250s already spent when helm starts
    ],
)
def test_helm_gets_the_per_hr_timeout_and_a_capped_subprocess(
    per_hr: float, total: float, late: bool, helm_timeout: str, cap: float
) -> None:
    runner = cluster(helmrelease())

    def clock() -> float:
        return 250.0 if late and calls(runner, "kubectl", "-n", "loki", "get", "pods") else 0.0

    _test(runner, clock=clock, per_hr_timeout_seconds=per_hr, total_timeout_seconds=total)

    [record] = [r for r in runner.records if r.args[:2] == ("helm", "test")]
    assert plain_argv(record.args) == (*HELM_TEST, "--timeout", helm_timeout, "--logs")
    assert record.timeout == cap


@pytest.mark.parametrize(
    ("release", "pods", "helm", "phases"),
    [
        (helmrelease(), items(), Reply(), ["Preflight", "Reaping", "Running", "Finished"]),
        (helmrelease(), items(), TEST_FAILED, ["Preflight", "Reaping", "Running", "Finished"]),
        (helmrelease(suspend=True), items(), Reply(), ["Preflight"]),
        (helmrelease(), items(pod("x", "Running")), Reply(), ["Preflight", "Reaping"]),
    ],
)
def test_progress_hears_each_phase(
    release: dict[str, Any], pods: Reply, helm: Reply, phases: list[str]
) -> None:
    runner = _helm(cluster(release), helm)
    runner.respond_each(hook_pods, pods)
    seen: list[ProgressEvent | RowUpdate] = []

    _test(runner, progress=seen.append)

    assert seen == [RowUpdate(("loki", "loki"), "phase", phase, ANY) for phase in phases]


def test_a_raising_progress_callback_does_not_break_the_run() -> None:
    def explode(_event: ProgressEvent | RowUpdate) -> None:
        raise RuntimeError("callback boom")

    assert [o.verdict for o in _test(cluster(helmrelease()), progress=explode).outcomes] == [
        "passed"
    ]


def test_the_report_keeps_five_pods_and_truncates_each_stream() -> None:
    big = "x" * 20_000
    out, err = "o" * 40_000, "e" * 50_000
    runner = _helm(cluster(helmrelease()), Reply(returncode=1, stdout=out, stderr=err))
    runner.respond_each(
        hook_pods, items(), items(*(pod(f"loki-test-{i}", "Failed") for i in range(7)))
    )
    runner.respond(lambda argv: "--previous" in argv, stdout=big)
    runner.respond(argv_prefix("kubectl", "-n", "loki", "logs", "loki-test-0"), stdout=big)
    runner.respond(argv_prefix("kubectl", "-n", "loki", "logs"), stdout="")

    [outcome] = _test(runner).outcomes

    assert [p.name for p in outcome.test_pods] == [f"loki-test-{i}" for i in range(5)]
    current, restarted = outcome.test_pods[:2]
    assert current.logs == truncate_bytes(big, 16_384)
    assert restarted.previous_logs == truncate_bytes(big, 16_384)
    assert outcome.helm_test_stdout == truncate_bytes(out, 32_768)
    assert outcome.helm_test_stderr == truncate_bytes(err, 32_768)
