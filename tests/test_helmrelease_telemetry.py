"""Promotion-lifecycle telemetry emitted by MonitorService and TestService.

Before this, `PromotionPhase.WAITING_ROLLOUT`, `ROLLOUT_OK`, `HELM_TEST_RUN`,
`HELM_TEST_OK`, `HELM_TEST_FAILED` and `PROMOTED` were emitted nowhere, so the
promotion timeline had a start (`FLUX_PR_OPEN`, from `promote.py`) and no end
and DESIGN.md's "duration from renovate PR propagation to all envs" could not
be computed. The wiring was entirely unguarded; this module is that guard.

Fakes are local rather than imported from the monitor/test service test
modules: those files are slated for a de-brittling pass, and a shared double
would make this suite fail for reasons that have nothing to do with
telemetry.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from chart_manager.integrations.helmrelease import (
    ConditionSnapshot,
    HelmReleaseRef,
    HelmReleaseStatus,
)
from chart_manager.plumbing.commands import CommandResult
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.events.lifecycle import PromotionPhase
from chart_manager.services.helmrelease.monitor import MonitorRequest, MonitorService
from chart_manager.services.helmrelease.state import (
    PROMOTE_PHASE,
    TERMINAL_PHASES,
    PromoteStatus,
    Stage,
    Verdict,
    run_verdict,
)
from chart_manager.services.helmrelease.test import TestRequest, TestService

CHART = "loki"
VERSION = "0.2.0"
ENV = "dev"
WALL = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)


# ----- doubles -------------------------------------------------------------


@dataclass
class _RecordingEvents:
    """EventWriter stand-in that records every promotion event it is handed."""

    events: list[dict[str, Any]] = field(default_factory=list)
    raises: BaseException | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def promote(self, **kwargs: Any) -> None:
        with self.lock:
            self.events.append(kwargs)
        if self.raises is not None:
            raise self.raises

    @property
    def phases(self) -> list[PromotionPhase]:
        return [e["phase"] for e in self.events]


def _ref(name: str = "loki", namespace: str = "loki") -> HelmReleaseRef:
    return HelmReleaseRef(
        name=name,
        namespace=namespace,
        api_version="helm.toolkit.fluxcd.io/v2",
        release_name=name,
        storage_namespace=namespace,
        target_namespace=namespace,
    )


def _status(
    ref: HelmReleaseRef,
    *,
    ready: str = "True",
    ready_reason: str = "ReconciliationSucceeded",
    released: str = "True",
    suspended: bool = False,
) -> HelmReleaseStatus:
    return HelmReleaseStatus(
        ref=ref,
        observed_at=WALL,
        generation=1,
        observed_generation=1,
        resource_version="1",
        suspended=suspended,
        desired_chart_name=CHART,
        desired_chart_version=VERSION,
        last_applied_revision=None,
        history_chart_version=VERSION,
        conditions=(
            ConditionSnapshot(
                type="Ready",
                status=ready,
                reason=ready_reason,
                message="msg",
                last_transition_time=None,
            ),
            ConditionSnapshot(
                type="Released",
                status=released,
                reason="InstallSucceeded",
                message="msg",
                last_transition_time=None,
            ),
        ),
    )


@dataclass
class _FakeCluster:
    """Minimal cluster double: enough for matching, one poll, and diagnostics."""

    list_result: list[HelmReleaseRef] = field(default_factory=list)
    statuses: dict[tuple[str, str], HelmReleaseStatus] = field(default_factory=dict)
    workloads_exc: BaseException | None = None
    test_pods: list[tuple[str, str, str]] = field(default_factory=list)

    def list(
        self, *, namespace: str | None = None, timeout: float | None = None
    ) -> list[HelmReleaseRef]:
        return list(self.list_result)

    def get_status(
        self, ref: HelmReleaseRef, *, timeout: float | None = None
    ) -> HelmReleaseStatus:
        return self.statuses[(ref.namespace, ref.name)]

    def list_owned_workloads(
        self, ref: HelmReleaseRef, *, timeout: float | None = None
    ) -> tuple[Any, ...]:
        if self.workloads_exc is not None:
            raise self.workloads_exc
        return ()

    def list_test_pods(
        self, ref: HelmReleaseRef, *, timeout: float | None = None
    ) -> list[tuple[str, str, str]]:
        return list(self.test_pods)

    def delete_pod(self, ns: str, name: str, *, timeout: float | None = None) -> None:
        return None

    def pod_logs(self, *args: Any, **kwargs: Any) -> str:
        return "logs"

    def namespace_events(self, namespace: str, *, timeout: float | None = None) -> str:
        return "ns evt"

    def workload_events(self, *args: Any, **kwargs: Any) -> str:
        return "wl evt"


@dataclass
class _FakeHelm:
    """helm double: one canned `helm test` result, or an exception."""

    result: CommandResult = field(
        default_factory=lambda: CommandResult(args=(), returncode=0, stdout="", stderr="")
    )

    def test(self, *args: Any, **kwargs: Any) -> CommandResult:
        return self.result


class _Clock:
    """Monotonic clock frozen at zero -- budgets never expire mid-test."""

    def __call__(self) -> float:
        return 0.0


# ----- builders ------------------------------------------------------------


def _monitor(
    cluster: _FakeCluster, events: _RecordingEvents, *, strict: bool = False
) -> MonitorService:
    return MonitorService(
        cluster,  # type: ignore[arg-type]
        kubectl=cluster,  # type: ignore[arg-type]
        sleep=lambda _t: None,
        clock=_Clock(),
        now=lambda: WALL,
        rand=lambda _lo, _hi: 0.0,
        events=events,  # type: ignore[arg-type]
        strict_events=strict,
    )


def _tester(
    cluster: _FakeCluster, helm: _FakeHelm, events: _RecordingEvents, *, strict: bool = False
) -> TestService:
    return TestService(
        cluster,  # type: ignore[arg-type]
        helm,  # type: ignore[arg-type]
        kubectl=cluster,  # type: ignore[arg-type]
        clock=_Clock(),
        now=lambda: WALL,
        events=events,  # type: ignore[arg-type]
        strict_events=strict,
    )


def _one_ready_hr() -> _FakeCluster:
    ref = _ref()
    return _FakeCluster(list_result=[ref], statuses={("loki", "loki"): _status(ref)})


def _monitor_req(**overrides: Any) -> MonitorRequest:
    base: dict[str, Any] = {
        "chart_name": CHART,
        "version": VERSION,
        "poll_interval": 1.0,
        "environment": ENV,
    }
    base.update(overrides)
    return MonitorRequest(**base)


def _test_req(**overrides: Any) -> TestRequest:
    base: dict[str, Any] = {
        "chart_name": CHART,
        "version": VERSION,
        "per_hr_timeout": "1m",
        "total_timeout": "5m",
        "subprocess_slack": "5s",
        "environment": ENV,
    }
    base.update(overrides)
    return TestRequest(**base)


# ----- monitor: the rollout half of the interval ---------------------------


def test_monitor_brackets_a_converged_rollout() -> None:
    events = _RecordingEvents()
    result = _monitor(_one_ready_hr(), events).monitor(_monitor_req())

    assert result.ok is True
    assert events.phases == [
        PromotionPhase.WAITING_ROLLOUT,
        PromotionPhase.ROLLOUT_OK,
    ]
    opened, closed = events.events
    # correlation_id is derived by EventWriter from chart@version; the
    # environment is what scopes the interval to one promotion target.
    assert opened["chart_name"] == CHART
    assert opened["chart_version"] == VERSION
    assert opened["environment"] == ENV
    assert opened["detail"] == {"stage": "rollout", "matched": 1}
    assert closed["detail"] == {
        "stage": "rollout",
        "verdict": "ready",
        "total": 1,
        "failures": 0,
    }


def test_monitor_emits_nothing_without_an_environment() -> None:
    # The default for an ad-hoc `helmrelease monitor`. Inventing a placeholder
    # environment would put an unattached interval on a real timeline.
    events = _RecordingEvents()
    _monitor(_one_ready_hr(), events).monitor(_monitor_req(environment=None))
    assert events.events == []


def test_monitor_emits_nothing_when_nothing_matched() -> None:
    # No interval was opened, so there is no bracket to close. A phantom
    # WAITING_ROLLOUT here would never be closed by anything.
    ref = _ref()
    cluster = _FakeCluster(
        list_result=[ref],
        statuses={("loki", "loki"): _status(ref)},
    )
    events = _RecordingEvents()
    _monitor(cluster, events).monitor(_monitor_req(version="9.9.9"))
    assert events.events == []


def test_monitor_closes_a_failed_rollout_with_abandoned() -> None:
    ref = _ref()
    cluster = _FakeCluster(
        list_result=[ref],
        statuses={("loki", "loki"): _status(ref, ready="False", ready_reason="InstallFailed")},
    )
    events = _RecordingEvents()
    result = _monitor(cluster, events).monitor(_monitor_req())

    assert result.ok is False
    assert events.phases == [
        PromotionPhase.WAITING_ROLLOUT,
        PromotionPhase.ABANDONED,
    ]
    assert events.events[1]["detail"]["verdict"] == "failed"
    assert events.events[1]["detail"]["failures"] == 1


def test_monitor_closes_the_interval_when_a_watcher_raises() -> None:
    # An infrastructure failure propagates instead of producing outcomes, and
    # used to leave WAITING_ROLLOUT open forever -- exactly the dangling
    # bracket this wiring exists to remove.
    ref = _ref()
    cluster = _FakeCluster(
        list_result=[ref],
        statuses={("loki", "loki"): _status(ref)},
        workloads_exc=ChartManagerError("kubectl exploded"),
    )
    events = _RecordingEvents()
    with pytest.raises(ChartManagerError):
        _monitor(cluster, events).monitor(_monitor_req())

    assert events.phases == [
        PromotionPhase.WAITING_ROLLOUT,
        PromotionPhase.ABANDONED,
    ]
    # The releases that never reported are counted as failures, not as zero.
    assert events.events[1]["detail"] == {
        "stage": "rollout",
        "verdict": "failed",
        "total": 1,
        "failures": 1,
    }


# ----- test service: the verification half ---------------------------------


def test_helm_test_pass_also_reports_promoted() -> None:
    # A green helm test is what makes the promotion *verified* live, so it
    # closes the promotion as well as the test interval.
    events = _RecordingEvents()
    result = _tester(_one_ready_hr(), _FakeHelm(), events).test(_test_req())

    assert result.ok is True
    assert events.phases == [
        PromotionPhase.HELM_TEST_RUN,
        PromotionPhase.HELM_TEST_OK,
        PromotionPhase.PROMOTED,
    ]
    assert events.events[0]["detail"] == {"stage": "helm-test", "matched": 1}


def test_helm_test_failure_closes_with_helm_test_failed_and_no_promoted() -> None:
    events = _RecordingEvents()
    helm = _FakeHelm(
        result=CommandResult(args=(), returncode=1, stdout="", stderr="pod failed")
    )
    result = _tester(_one_ready_hr(), helm, events).test(_test_req())

    assert result.ok is False
    assert events.phases == [
        PromotionPhase.HELM_TEST_RUN,
        PromotionPhase.HELM_TEST_FAILED,
    ]
    assert events.events[1]["detail"]["verdict"] == "failed"


def test_all_suspended_helm_test_run_emits_no_terminal_phase() -> None:
    """Zero tests ran, so nothing was verified and nothing was promoted.

    `SKIPPED_SUSPENDED` is a passing verdict, which used to make an
    all-suspended run fold to `PASSED` and emit (HELM_TEST_OK, PROMOTED) --
    recording a version as verified-live in the environment when helm was
    never invoked. The interval still opens; it just has no terminal.
    """
    ref = _ref()
    cluster = _FakeCluster(
        list_result=[ref],
        statuses={("loki", "loki"): _status(ref, suspended=True)},
    )
    events = _RecordingEvents()
    result = _tester(cluster, _FakeHelm(), events).test(_test_req())

    assert [o.verdict for o in result.outcomes] == [Verdict.SKIPPED_SUSPENDED]
    assert events.phases == [PromotionPhase.HELM_TEST_RUN]


def test_all_suspended_rollout_emits_no_terminal_phase() -> None:
    # Same contract on the monitor half: a rollout nobody was watching for
    # is not a ROLLOUT_OK.
    ref = _ref()
    cluster = _FakeCluster(
        list_result=[ref],
        statuses={("loki", "loki"): _status(ref, suspended=True)},
    )
    events = _RecordingEvents()
    result = _monitor(cluster, events).monitor(_monitor_req())

    assert [o.verdict for o in result.outcomes] == [Verdict.SKIPPED_SUSPENDED]
    assert events.phases == [PromotionPhase.WAITING_ROLLOUT]


def test_a_skip_alongside_a_real_pass_still_reports_the_pass() -> None:
    # The narrow reading of the rule above: only an *entirely* suspended run
    # is a non-transition. One suspended peer must not suppress the terminal
    # phase for the release that actually went green.
    assert (
        run_verdict(
            [Verdict.SKIPPED_SUSPENDED, Verdict.PASSED], success=Verdict.PASSED
        )
        is Verdict.PASSED
    )


def test_helm_test_emits_nothing_without_an_environment() -> None:
    events = _RecordingEvents()
    _tester(_one_ready_hr(), _FakeHelm(), events).test(_test_req(environment=None))
    assert events.events == []


# ----- failure policy ------------------------------------------------------


def test_event_emission_failure_does_not_break_the_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Same policy as PromoteService: the rollout already happened, so an
    # unconfigured backend must not turn a converged run into a traceback.
    events = _RecordingEvents(raises=KeyError("COSMOS_ENDPOINT"))
    result = _monitor(_one_ready_hr(), events).monitor(_monitor_req())

    assert result.ok is True
    assert len(events.events) == 2  # both attempted, both swallowed
    assert "non-fatal" in caplog.text


def test_strict_events_surfaces_the_emission_failure() -> None:
    # Opt-in for callers where the event IS the deliverable (e.g. a backfill).
    events = _RecordingEvents(raises=KeyError("COSMOS_ENDPOINT"))
    with pytest.raises(KeyError):
        _monitor(_one_ready_hr(), events, strict=True).monitor(_monitor_req())


# ----- the tables themselves ------------------------------------------------


def test_every_terminal_phase_pair_is_reachable_from_a_run_verdict() -> None:
    """No entry in TERMINAL_PHASES describes a verdict a run cannot produce.

    `run_verdict` folds per-HelmRelease verdicts into one; a table row keyed
    on a verdict that fold can never return would be dead configuration that
    reads as if the timeline covered a case it does not.
    """
    reachable = {
        (Stage.ROLLOUT, run_verdict([v], success=Verdict.READY)) for v in Verdict
    } | {(Stage.HELM_TEST, run_verdict([v], success=Verdict.PASSED)) for v in Verdict}
    assert set(TERMINAL_PHASES) <= reachable


def test_promoted_is_only_reported_after_a_green_helm_test() -> None:
    # A monitor-only pipeline must never claim PROMOTED: nothing has checked
    # that the workload actually works.
    emitting_promoted = {
        key for key, phases in TERMINAL_PHASES.items() if PromotionPhase.PROMOTED in phases
    }
    assert emitting_promoted == {(Stage.HELM_TEST, Verdict.PASSED)}


def test_no_promote_status_is_missing_a_phase_decision() -> None:
    """Every PromoteStatus must appear in PROMOTE_PHASE, explicitly.

    A missing key raises KeyError in `_emit_promotion` rather than silently
    dropping the event, but this catches it at the table instead of on the
    one code path that runs after a PR is already open.
    """
    assert set(PROMOTE_PHASE) == set(PromoteStatus)
