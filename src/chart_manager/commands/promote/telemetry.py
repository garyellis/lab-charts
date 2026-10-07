"""Promotion-lifecycle telemetry for `promote pr`, `promote monitor` and `promote test`.

`emit_promotion` is the single write path for promotion events: every stage
reaches `EventWriter.promote` through it, under the non-fatal failure policy.

`monitor.run` and `test.run` are the only components that know when a
rollout starts, when it converges, and whether `helm test` went green -- but
neither could reach `EventWriter`, so `PromotionPhase.WAITING_ROLLOUT`,
`ROLLOUT_OK` and `HELM_TEST_*` were emitted nowhere. The promotion timeline
had a start (`FLUX_PR_OPEN`, from `commands/promote/pr.py`) and no end, which
is exactly what makes the "duration from renovate PR propagation to
all envs" uncomputable.

The *failure policy* itself lives in `shared/events/failure.py`, shared with
the other event emitters.
"""
from __future__ import annotations

from dataclasses import dataclass

from chart_manager.shared.events.failure import emit_non_fatal
from chart_manager.shared.events.model import PromotionPhase
from chart_manager.shared.events.writer import EventWriter

from .state import START_PHASE, TERMINAL_PHASES, Stage, Verdict

__all__ = ["PromotionTelemetry", "emit_promotion"]


def emit_promotion(
    writer: EventWriter,
    *,
    chart_name: str,
    chart_version: str,
    environment: str,
    phase: PromotionPhase,
    what: str,
    pr_url: str | None = None,
    promotion_correlation_id: str | None = None,
    detail: dict[str, object] | None = None,
) -> None:
    """Write one promotion event; a failed write is logged under `what`, not raised."""
    emit_non_fatal(
        lambda: writer.promote(
            chart_name=chart_name,
            chart_version=chart_version,
            environment=environment,
            phase=phase,
            pr_url=pr_url,
            promotion_correlation_id=promotion_correlation_id,
            detail=detail,
        ),
        strict=False,
        what=what,
    )


@dataclass(frozen=True)
class PromotionTelemetry:
    """Emits promotion events for one monitor/test run.

    Bound to a single (chart, version, environment) triple because that is
    the grain of the timeline: `EventWriter` derives `correlation_id` from
    chart@version and the environment scopes it to one promotion target.

    Disabled -- every method a silent no-op -- when `environment` is None.
    `EventWriter.promote` requires an environment, and a run invoked without
    one (the default for an ad-hoc `promote monitor`) is not part of any
    promotion, so inventing a placeholder would corrupt the timeline it is
    meant to measure.
    """

    writer: EventWriter
    chart_name: str
    version: str
    environment: str | None = None

    def started(self, stage: Stage, *, matched: int) -> None:
        """Open the interval for `stage` (WAITING_ROLLOUT / HELM_TEST_RUN)."""
        self._emit(START_PHASE[stage], {"stage": str(stage), "matched": matched})

    def finished(
        self, stage: Stage, verdict: Verdict, *, total: int, failures: int
    ) -> None:
        """Close the interval for `stage` with the run-level `verdict`.

        Emits nothing for verdicts that record no transition (all-skipped, or
        no HelmRelease matched) -- see `state.TERMINAL_PHASES`.
        """
        detail = {
            "stage": str(stage),
            "verdict": str(verdict),
            "total": total,
            "failures": failures,
        }
        for phase in TERMINAL_PHASES.get((stage, verdict), ()):
            self._emit(phase, detail)

    def _emit(self, phase: PromotionPhase, detail: dict[str, object]) -> None:
        """Write one event, honoring the enabled check and the failure policy."""
        if self.environment is None:
            return

        # detail carries only str/int/bool: `DynamoDBEventStore` hands the
        # item straight to boto3, whose serializer rejects float. Durations
        # are deliberately absent -- they are the difference between two
        # event timestamps, which is the whole reason these events exist.
        emit_promotion(
            self.writer,
            chart_name=self.chart_name,
            chart_version=self.version,
            environment=self.environment,
            phase=phase,
            what=f"promotion {phase.value}",
            detail=detail,
        )
