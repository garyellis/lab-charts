"""How `run_fanout` classifies a worker that raised.

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
from dataclasses import dataclass

import pytest

from chart_manager.integrations.helmrelease import HelmReleaseRef, HelmReleaseStatus
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.helmrelease.fanout import run_fanout, sorted_by_ref


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
