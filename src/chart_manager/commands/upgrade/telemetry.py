"""Build-lifecycle telemetry for `chart upgrade`.

A run that changes the version its pull request proposes emits `BuildPhase.PR_OPEN`. `pr_updated`
emits it too, because a re-run can retarget the version (patch to major) and every version's
timeline must start with `PR_OPEN`.
"""

from __future__ import annotations

from dataclasses import dataclass

from chart_manager.shared.events.failure import emit_non_fatal
from chart_manager.shared.events.model import BuildPhase
from chart_manager.shared.events.writer import EventWriter

from .models import UpgradeResult, UpgradeStatus

__all__ = ["UpgradeTelemetry"]

_PROPOSED = {UpgradeStatus.PR_OPEN, UpgradeStatus.PR_UPDATED}


@dataclass(frozen=True)
class UpgradeTelemetry:
    """Emits the build-lifecycle event for one completed upgrade run."""

    writer: EventWriter

    def completed(
        self, result: UpgradeResult, *, previously_proposed: str | None
    ) -> None:
        """Emit the phase for `result`, or nothing if it records no transition.

        `previously_proposed` is the wrapper version already on the open
        branch before this run; None when no pull request was open.
        """
        if result.outcome not in _PROPOSED:
            return

        # An unchanged proposal is a re-run, not a transition.
        if previously_proposed is not None and previously_proposed == result.proposed_version:
            return

        # Without a version the correlation id would be "<chart>@None"; diagnostics say why.
        if result.proposed_version is None:
            return

        # "{repository}#{pr_number}" is what later CI events can rebuild from the
        # GitHub Actions context, so they land on the same build.
        build_correlation_id = (
            f"{result.repository}#{result.pr_number}"
            if result.repository and result.pr_number is not None
            else None
        )

        # str/int/bool only: boto3's DynamoDB serializer rejects float.
        detail: dict[str, object] = {
            "outcome": result.outcome,
            "previous_version": result.current_version,
            "group": result.group,
        }
        if result.branch:
            detail["branch"] = result.branch

        emit_non_fatal(
            lambda: self.writer.build(
                chart_name=result.chart,
                chart_version=result.proposed_version,
                phase=BuildPhase.PR_OPEN,
                build_correlation_id=build_correlation_id,
                pr_url=result.pr_url,
                detail=detail,
            ),
            strict=False,
            what=f"build {BuildPhase.PR_OPEN.value}",
        )
