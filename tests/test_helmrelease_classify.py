"""Direct coverage for the pure rollout classifier.

`classify` is the branch table `MonitorService._watch_one` used to inline.
Driving it through the watcher meant every rule cost a scripted cluster fake, a
clock and a thread pool; here each rule is one status literal and one
assertion, so the Flux condition semantics can be reviewed as a table.

The watcher-level tests in `test_helmrelease_monitor_service.py` still cover
the *loop* -- backoff, budgets, cancellation, dedupe across polls. This file
covers only what a single status snapshot means.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from chart_manager.integrations.helmrelease import (
    ConditionSnapshot,
    HelmReleaseRef,
    HelmReleaseStatus,
    OwnedWorkload,
    WorkloadRollout,
)
from chart_manager.services.helmrelease.classify import Terminal, Waiting, classify
from chart_manager.services.helmrelease.state import DETAIL_MAX, Reason, Verdict

VERSION = "0.2.0"
WALL = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)

REF = HelmReleaseRef(
    name="loki",
    namespace="loki",
    api_version="helm.toolkit.fluxcd.io/v2",
    release_name="loki",
    storage_namespace="loki",
    target_namespace="loki",
)


def _cond(type_: str, status: str, *, reason: str = "", message: str = "") -> ConditionSnapshot:
    return ConditionSnapshot(
        type=type_, status=status, reason=reason, message=message, last_transition_time=WALL
    )


def _status(
    *,
    generation: int = 1,
    observed_generation: int = 1,
    suspended: bool = False,
    history_chart_version: str | None = VERSION,
    conditions: tuple[ConditionSnapshot, ...] = (),
) -> HelmReleaseStatus:
    return HelmReleaseStatus(
        ref=REF,
        observed_at=WALL,
        generation=generation,
        observed_generation=observed_generation,
        resource_version="1",
        suspended=suspended,
        desired_chart_name="loki",
        desired_chart_version=VERSION,
        last_applied_revision=None,
        history_chart_version=history_chart_version,
        conditions=conditions,
    )


def _converged_hr(**overrides: object) -> HelmReleaseStatus:
    """A status the HelmRelease itself calls done: gen caught up, history matches, Ready=True."""
    kwargs: dict[str, object] = {
        "conditions": (_cond("Ready", "True", reason="ReconciliationSucceeded"),)
    }
    kwargs.update(overrides)
    return _status(**kwargs)  # type: ignore[arg-type]


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


def _classify(status: HelmReleaseStatus, workloads: tuple[WorkloadRollout, ...] | None = None):
    return classify(status, requested_version=VERSION, workloads=workloads)


# ----- suspension ----------------------------------------------------------


def test_suspended_is_terminal_and_skipped_not_failed() -> None:
    decision = _classify(_status(suspended=True))
    assert decision == Terminal(
        verdict=Verdict.SKIPPED_SUSPENDED,
        reason=Reason.SUSPENDED,
        phase="Suspended",
        detail="HR spec.suspend=true",
    )


def test_suspension_outranks_a_stalled_condition() -> None:
    """A suspended release was taken out of the rollout deliberately.

    Reporting the Stalled it was suspended *because of* would fail a run the
    operator explicitly opted out of.
    """
    decision = _classify(
        _status(suspended=True, conditions=(_cond("Stalled", "True", message="stuck"),))
    )
    assert isinstance(decision, Terminal)
    assert decision.verdict is Verdict.SKIPPED_SUSPENDED


# ----- stalled -------------------------------------------------------------


def test_stalled_true_is_terminal_failure() -> None:
    decision = _classify(_status(conditions=(_cond("Stalled", "True", message="stuck"),)))
    assert isinstance(decision, Terminal)
    assert (decision.verdict, decision.reason) == (Verdict.FAILED, Reason.STALLED)
    assert decision.phase == "Stalled"
    assert decision.detail == "stuck"


def test_stalled_false_is_not_terminal() -> None:
    decision = _classify(
        _status(
            generation=2,
            observed_generation=1,
            conditions=(_cond("Stalled", "False"),),
        )
    )
    assert isinstance(decision, Waiting)


def test_stalled_message_is_capped() -> None:
    decision = _classify(
        _status(conditions=(_cond("Stalled", "True", message="x" * 500),))
    )
    assert isinstance(decision, Terminal)
    assert len(decision.detail) == DETAIL_MAX


# ----- terminal Ready reasons ---------------------------------------------


@pytest.mark.parametrize(
    "flux_reason",
    [
        "InstallFailed",
        "UpgradeFailed",
        "ReconciliationFailed",
        "ArtifactFailed",
        "RetryExhausted",
    ],
)
def test_ready_false_with_terminal_reason_fails(flux_reason: str) -> None:
    decision = _classify(
        _status(conditions=(_cond("Ready", "False", reason=flux_reason, message="bad"),))
    )
    assert isinstance(decision, Terminal)
    assert decision.verdict is Verdict.FAILED
    # The Flux reason is passed through, not translated: it is what the CRD
    # says and what an operator will grep the cluster for.
    assert decision.reason == flux_reason
    assert decision.phase == f"Ready=False:{flux_reason}"


def test_ready_false_with_a_retryable_reason_keeps_waiting() -> None:
    """Flux retries out of these, so the watcher must not declare a failure."""
    decision = _classify(
        _status(conditions=(_cond("Ready", "False", reason="Progressing"),))
    )
    assert isinstance(decision, Waiting)
    assert decision.phase == "WaitingForReady:Progressing"


# ----- TestSuccess ---------------------------------------------------------


def test_test_success_false_is_terminal_only_once_released() -> None:
    conditions = (
        _cond("Ready", "True", reason="ReconciliationSucceeded"),
        _cond("TestSuccess", "False", reason="TestFailed", message="probe died"),
        _cond("Released", "True", reason="UpgradeSucceeded"),
    )
    decision = _classify(_status(conditions=conditions))
    assert isinstance(decision, Terminal)
    assert (decision.verdict, decision.reason) == (Verdict.FAILED, Reason.TEST_FAILED)
    assert decision.phase == "TestSuccess=False"
    assert decision.detail == "probe died"


def test_test_success_false_before_released_is_only_stale_hook_state() -> None:
    conditions = (
        _cond("Ready", "True", reason="ReconciliationSucceeded"),
        _cond("TestSuccess", "False", reason="TestFailed"),
        _cond("Released", "False"),
    )
    decision = _classify(_status(conditions=conditions), (_workload(),))
    # Ready + gen + history all line up and the workload is converged, so the
    # release is genuinely done -- the pre-run TestSuccess must not veto it.
    assert isinstance(decision, Terminal)
    assert decision.verdict is Verdict.READY


def test_test_success_without_a_reason_falls_back_to_test_failed() -> None:
    conditions = (
        _cond("Ready", "True"),
        _cond("TestSuccess", "False", reason=""),
        _cond("Released", "True"),
    )
    decision = _classify(_status(conditions=conditions))
    assert isinstance(decision, Terminal)
    assert decision.reason is Reason.TEST_FAILED


def test_unmodelled_flux_test_reason_survives_as_a_plain_string() -> None:
    """`ReasonLike` has an open tail; an unknown reason must not raise."""
    conditions = (
        _cond("Ready", "True"),
        _cond("TestSuccess", "False", reason="SomethingFluxShippedLater"),
        _cond("Released", "True"),
    )
    decision = _classify(_status(conditions=conditions))
    assert isinstance(decision, Terminal)
    assert decision.reason == "SomethingFluxShippedLater"
    assert not isinstance(decision.reason, Reason)


# ----- waiting phases ------------------------------------------------------


def test_generation_lag_waits_and_does_not_ask_for_workloads() -> None:
    decision = _classify(
        _converged_hr(generation=2, observed_generation=1)
    )
    assert isinstance(decision, Waiting)
    assert decision.phase == "GenerationLag"
    assert decision.needs_workloads is False


def test_history_mismatch_waits_and_does_not_ask_for_workloads() -> None:
    decision = _classify(_converged_hr(history_chart_version="0.1.0"))
    assert isinstance(decision, Waiting)
    assert decision.phase == "HistoryLag"
    assert decision.needs_workloads is False


def test_absent_ready_condition_waits() -> None:
    decision = _classify(_status(conditions=()))
    assert isinstance(decision, Waiting)
    assert decision.phase == "WaitingForReady"


def test_waiting_detail_reports_gen_history_and_request() -> None:
    decision = _classify(_converged_hr(generation=2, observed_generation=1))
    assert isinstance(decision, Waiting)
    assert "obs-gen=1/2" in decision.detail
    assert f"requested={VERSION}" in decision.detail


# ----- workloads -----------------------------------------------------------


def test_converged_hr_without_workloads_read_asks_for_them() -> None:
    decision = _classify(_converged_hr(), workloads=None)
    assert isinstance(decision, Waiting)
    assert decision.needs_workloads is True


def test_converged_hr_with_all_workloads_converged_is_ready() -> None:
    decision = _classify(_converged_hr(), (_workload(),))
    assert decision == Terminal(
        verdict=Verdict.READY,
        reason=Reason.READY,
        phase="Ready",
        detail="HR Ready=True and all workloads converged",
    )


def test_converged_hr_with_pending_workloads_waits_and_names_them() -> None:
    decision = _classify(
        _converged_hr(),
        (_workload("a", converged=False), _workload("b", converged=False), _workload("c")),
    )
    assert isinstance(decision, Waiting)
    assert decision.phase == "WaitingForWorkloads:2"
    assert "pending=[Deployment/loki/a,Deployment/loki/b]" in decision.detail
    assert decision.needs_workloads is False


def test_empty_workload_list_reads_as_converged() -> None:
    """Documents current behavior, which discovery F12b flags as a real gap.

    A chart with no Deployments legitimately lands here, but so does a
    just-upgraded release whose new Deployment has not yet acquired the
    `helm.toolkit.fluxcd.io/name` label the selector matches on. This test
    pins the behavior so a fix has to change it deliberately.
    """
    decision = _classify(_converged_hr(), ())
    assert isinstance(decision, Terminal)
    assert decision.verdict is Verdict.READY


# ----- dedupe signature ----------------------------------------------------


def test_identical_statuses_produce_an_identical_signature() -> None:
    a = _classify(_converged_hr(generation=2, observed_generation=1))
    b = _classify(_converged_hr(generation=2, observed_generation=1))
    assert isinstance(a, Waiting) and isinstance(b, Waiting)
    assert a.signature == b.signature


def test_signature_changes_when_the_observed_generation_moves() -> None:
    a = _classify(_converged_hr(generation=3, observed_generation=1))
    b = _classify(_converged_hr(generation=3, observed_generation=2))
    assert isinstance(a, Waiting) and isinstance(b, Waiting)
    assert a.signature != b.signature


def test_signature_changes_when_the_pending_workload_set_changes() -> None:
    a = _classify(_converged_hr(), (_workload("a", converged=False),))
    b = _classify(
        _converged_hr(), (_workload("a", converged=False), _workload("b", converged=False))
    )
    assert isinstance(a, Waiting) and isinstance(b, Waiting)
    assert a.signature != b.signature


def test_signature_ignores_pending_workload_ordering() -> None:
    """The set of stuck workloads is the situation; the listing order is not."""
    a = _classify(
        _converged_hr(), (_workload("a", converged=False), _workload("b", converged=False))
    )
    b = _classify(
        _converged_hr(), (_workload("b", converged=False), _workload("a", converged=False))
    )
    assert isinstance(a, Waiting) and isinstance(b, Waiting)
    assert a.signature == b.signature
