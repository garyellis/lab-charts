"""One home for how a helmrelease run parallelises and how it cancels.

`MonitorService` and `TestService` each fan one worker out per matched
HelmRelease, collect outcomes as they land, and cancel their peers when the
budget runs out or a worker dies. That shell was written twice, verbatim down
to the `except BaseException` wrapper -- which meant the answer to "what
happens to the other seven releases when one watcher raises?" lived in two
places and could be fixed in one.

The only genuine difference was monitor's `fail_fast`, which is expressed
here as the `cancel_on` predicate: a caller decides *which* outcomes are bad
enough to stop the run, and this module decides *how* stopping works.

Not included, on purpose: the zero-match synthetic outcome and the aggregate
result. Both are per-service constructor calls over types with different
fields, so hoisting them would mean passing two factory callbacks to save
five lines of straight-line code at each call site.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Protocol

from chart_manager.integrations.helmrelease import HelmReleaseRef, HelmReleaseStatus
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError

__all__ = ["run_fanout", "sorted_by_ref"]


class HasRef(Protocol):
    """Any per-HelmRelease outcome; all this module needs is its identity.

    Structural rather than a shared base class: `MonitorOutcome` and
    `TestOutcome` are frozen dataclasses whose only common field is `ref`,
    and inheriting from a common parent for one attribute would put a
    coupling in the wire contract that nothing else needs.
    """

    @property
    def ref(self) -> HelmReleaseRef: ...


def _never(_outcome: object) -> bool:
    """Default `cancel_on`: let every worker run to its own conclusion."""
    return False


def run_fanout[OutcomeT: HasRef](
    matched: Sequence[HelmReleaseStatus],
    *,
    concurrency: int,
    clock: Callable[[], float],
    total_deadline: float,
    cancel_event: threading.Event,
    outcomes: list[OutcomeT],
    work: Callable[[HelmReleaseStatus], OutcomeT],
    crash_label: str,
    cancel_on: Callable[[OutcomeT], bool] = _never,
) -> None:
    """Run `work` per matched HelmRelease, appending outcomes as they complete.

    `outcomes` is filled in place rather than returned so that when a worker
    raises, the caller can still say how many releases were never accounted
    for -- which is what its lifecycle event needs to report.

    Cancellation is cooperative: `cancel_event` is a flag the workers poll,
    so a worker already blocked in a subprocess finishes that call first. The
    subprocess timeouts are what bound that, not this loop.
    """
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(work, status) for status in matched]
        for fut in as_completed(futures):
            try:
                outcome = fut.result()
            except (ExternalCommandError, ChartManagerError):
                # The run is over: this is infrastructure, not a release
                # verdict. Cancel first so peers stop before the raise
                # unwinds through the executor's shutdown.
                cancel_event.set()
                raise
            except Exception as exc:
                cancel_event.set()
                raise ChartManagerError(f"{crash_label} crashed: {exc!r}") from exc
            except BaseException:
                # KeyboardInterrupt / SystemExit, re-raised unwrapped. Wrapping
                # them made a Ctrl-C indistinguishable from a worker crash, so
                # the callers' `except Exception:` telemetry handlers put a
                # network write in front of the exit and the operator lost 130.
                cancel_event.set()
                raise
            outcomes.append(outcome)
            # Cancellation is checked after the append so the outcome that
            # triggered it is itself reported -- a fail-fast run that hid its
            # own first failure would be unusable.
            if cancel_on(outcome):
                cancel_event.set()
            if clock() >= total_deadline:
                cancel_event.set()


def sorted_by_ref[OutcomeT: HasRef](outcomes: Iterable[OutcomeT]) -> tuple[OutcomeT, ...]:
    """Order outcomes by (namespace, name).

    Completion order is thread-scheduling order, i.e. noise. Sorting makes two
    runs of the same release set diffable, which is what a CI log is for.
    """
    return tuple(sorted(outcomes, key=lambda o: (o.ref.namespace, o.ref.name)))
