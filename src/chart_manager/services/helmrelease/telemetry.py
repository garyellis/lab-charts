"""Promotion-lifecycle telemetry for the helmrelease services.

`MonitorService` and `TestService` are the only components that know when a
rollout starts, when it converges, and whether `helm test` went green -- but
neither could reach `EventWriter`, so `PromotionPhase.WAITING_ROLLOUT`,
`ROLLOUT_OK` and `HELM_TEST_*` were emitted nowhere. The promotion timeline
had a start (`FLUX_PR_OPEN`, from `promote.py`) and no end, which is exactly
what makes DESIGN.md's "duration from renovate PR propagation to all envs"
uncomputable.

This module holds the promotion wiring. The *failure policy* it used to own
now lives in `services/events/failure.py`, which grew a fourth caller (the
upgrade service) and no longer belongs to this domain.
"""
from __future__ import annotations

from dataclasses import dataclass

from chart_manager.services.events.failure import emit_non_fatal
from chart_manager.services.events.lifecycle import PromotionPhase
from chart_manager.services.events.writer import EventWriter

from .state import START_PHASE, TERMINAL_PHASES, Stage, Verdict

__all__ = ["PromotionTelemetry"]


@dataclass(frozen=True)
class PromotionTelemetry:
    """Emits promotion events for one monitor/test run.

    Bound to a single (chart, version, environment) triple because that is
    the grain of the timeline: `EventWriter` derives `correlation_id` from
    chart@version and the environment scopes it to one promotion target.

    Disabled -- every method a silent no-op -- when `environment` is None.
    `EventWriter.promote` requires an environment, and a run invoked without
    one (the default for an ad-hoc `helmrelease monitor`) is not part of any
    promotion, so inventing a placeholder would corrupt the timeline it is
    meant to measure.
    """

    writer: EventWriter
    chart_name: str
    version: str
    environment: str | None = None
    strict: bool = False

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
        environment = self.environment

        # detail carries only str/int/bool: the DynamoDB adapter hands the
        # item straight to boto3, whose serializer rejects float. Durations
        # are deliberately absent -- they are the difference between two
        # event timestamps, which is the whole reason these events exist.
        emit_non_fatal(
            lambda: self.writer.promote(
                chart_name=self.chart_name,
                chart_version=self.version,
                environment=environment,
                phase=phase,
                detail=detail,
            ),
            strict=self.strict,
            what=f"promotion {phase.value}",
        )
