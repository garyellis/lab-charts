"""One home for how a promotion stage parallelises, cancels, and reports.

`MonitorService` and `TestService` each fan one worker out per matched
HelmRelease, collect outcomes as they land, and cancel their peers when the
budget runs out or a worker dies. That shell was written twice, verbatim down
to the `except BaseException` wrapper -- which meant the answer to "what
happens to the other seven releases when one watcher raises?" lived in two
places and could be fixed in one.

The only genuine difference was monitor's `fail_fast`, which is expressed
here as the `cancel_on` predicate: a caller decides *which* outcomes are bad
enough to stop the run, and this module decides *how* stopping works.

`run_fanout` alone left the sequence *around* the executor duplicated: the
zero-match short circuit, the telemetry bracket that must close even when a
worker crashes, the `Exception`-but-not-`BaseException` boundary on that
close, and the aggregate result. `run_matched` owns that whole sequence and
`RunResult` is the one aggregate type. What still differs per service is a
parameter, not a code path: the synthetic no-match outcome is a `no_match`
factory, the lifecycle stage and the verdict that counts as success are
arguments, and the log lines are built from `log_label`. The services keep
only what really is theirs -- parsing their request, matching, and the
"run started" line whose fields differ.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Protocol

from chart_manager.commands.promote.state import (
    PASSING_VERDICTS,
    Stage,
    Verdict,
    run_verdict,
)
from chart_manager.commands.promote.telemetry import PromotionTelemetry
from chart_manager.integrations.helmrelease import HelmReleaseRef, HelmReleaseStatus
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError

__all__ = ["RunResult", "run_fanout", "run_matched", "sorted_by_ref"]

_LOG = logging.getLogger(__name__)


class HasRef(Protocol):
    """Any per-HelmRelease outcome; all this module needs is its identity.

    Structural rather than a shared base class: `MonitorOutcome` and
    `TestOutcome` are unrelated frozen dataclasses, and inheriting from a
    common parent for the one or two attributes this module reads would put
    a coupling in the wire contract that nothing else needs.
    """

    @property
    def ref(self) -> HelmReleaseRef: ...


class HasVerdict(HasRef, Protocol):
    """An outcome that also carries its verdict -- all `RunResult` reads.

    A sibling of `HasRef` rather than a widening of it: `run_fanout` and
    `sorted_by_ref` genuinely need only the identity, and their tests say so
    by passing an outcome that has nothing else.
    """

    @property
    def verdict(self) -> Verdict: ...


@dataclass(frozen=True)
class RunResult[OutcomeT: HasVerdict]:
    """Aggregate of every per-HelmRelease outcome of one monitor or test run.

    Generic over the outcome type so each service keeps its own public name
    as a plain alias (`MonitorResult = RunResult[MonitorOutcome]`). The alias
    is an assignment, not a PEP 695 `type` statement, because callers --
    tests included -- construct `MonitorResult(...)` directly and a
    `TypeAliasType` is not callable.
    """

    outcomes: tuple[OutcomeT, ...]
    total_duration_seconds: float
    total_timed_out: bool

    @property
    def ok(self) -> bool:
        """True only if there were outcomes and every one carries a passing verdict."""
        return bool(self.outcomes) and all(
            o.verdict in PASSING_VERDICTS for o in self.outcomes
        )

    @property
    def failures(self) -> tuple[OutcomeT, ...]:
        """Outcomes whose verdict is not a passing one."""
        return tuple(o for o in self.outcomes if o.verdict not in PASSING_VERDICTS)


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


def run_matched[OutcomeT: HasVerdict](
    matched: Sequence[HelmReleaseStatus],
    *,
    start: float,
    clock: Callable[[], float],
    total_deadline: float,
    concurrency: int,
    telemetry: PromotionTelemetry,
    stage: Stage,
    success: Verdict,
    no_match: Callable[[float], OutcomeT],
    work: Callable[[HelmReleaseStatus, threading.Event], OutcomeT],
    crash_label: str,
    log_label: str,
    chart_name: str,
    version: str,
    namespace: str | None,
    cancel_on: Callable[[OutcomeT], bool] = _never,
) -> RunResult[OutcomeT]:
    """Run one matched set to a `RunResult`, bracketing it with telemetry.

    - Nothing matched: a single synthetic outcome from `no_match(elapsed)`,
      and *no* telemetry -- no interval was opened, so there is none to close.
    - Otherwise `telemetry.started(stage)`, then `run_fanout` with a fresh
      cancel flag handed to every `work(status, cancel_event)` call.
    - A worker crash (any `Exception`) still closes the interval with
      `Verdict.FAILED`, counting every release that never reported as a
      failure, then re-raises.
    - A clean finish closes it with the folded run verdict, where `success`
      is what an all-green run reports for `stage` (READY / PASSED).

    `start` is the caller's clock reading from before it listed and matched
    releases, so the durations reported here include that work.
    """
    if not matched:
        # A no-match is the one outcome that looks like success to every
        # caller that reads it loosely and means nothing was watched at all
        # -- say which selector found nothing.
        _LOG.warning(
            "%s matched no HelmReleases: chart=%s version=%s namespace=%s",
            log_label,
            chart_name,
            version,
            namespace or "(all)",
        )
        elapsed = clock() - start
        return RunResult(
            outcomes=(no_match(elapsed),),
            total_duration_seconds=elapsed,
            total_timed_out=False,
        )

    # Emitted after the zero-match return: a run with nothing to watch
    # opened no interval, so bracketing it would put a start phase on the
    # timeline that nothing will ever close.
    telemetry.started(stage, matched=len(matched))

    cancel_event = threading.Event()
    outcomes: list[OutcomeT] = []

    try:
        run_fanout(
            matched,
            concurrency=concurrency,
            clock=clock,
            total_deadline=total_deadline,
            cancel_event=cancel_event,
            outcomes=outcomes,
            work=lambda status: work(status, cancel_event),
            crash_label=crash_label,
            cancel_on=cancel_on,
        )
    except Exception:
        _LOG.exception(
            "%s run crashed: chart=%s version=%s matched=%d completed=%d",
            log_label,
            chart_name,
            version,
            len(matched),
            len(outcomes),
        )
        # An infrastructure failure still ends the interval opened above.
        # Without this the timeline keeps a start phase that nothing ever
        # closes -- the exact defect this wiring exists to remove.
        #
        # `Exception`, not `BaseException`: Ctrl-C must kill a long
        # parallel run immediately, and this handler would put a network
        # write in front of the exit. An interrupted run genuinely has no
        # terminal state to report.
        telemetry.finished(
            stage,
            Verdict.FAILED,
            total=len(matched),
            failures=len(matched) - len(outcomes),
        )
        raise

    elapsed = clock() - start
    result = RunResult(
        outcomes=sorted_by_ref(outcomes),
        total_duration_seconds=elapsed,
        total_timed_out=cancel_event.is_set(),
    )
    telemetry.finished(
        stage,
        run_verdict((o.verdict for o in result.outcomes), success=success),
        total=len(result.outcomes),
        failures=len(result.failures),
    )
    _LOG.info(
        "%s run finished: chart=%s version=%s outcomes=%d failures=%d "
        "cancelled=%s elapsed=%.1fs",
        log_label,
        chart_name,
        version,
        len(result.outcomes),
        len(result.failures),
        result.total_timed_out,
        elapsed,
    )
    return result
