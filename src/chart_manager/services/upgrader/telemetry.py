"""Build-lifecycle telemetry for the upgrade service.

`UpgradeService` is the only component that knows both halves of the fact a
`BuildPhase.PR_OPEN` event asserts: the wrapper version the finalizer decided
(read back off the pushed branch) and the pull request Renovate opened for it.
The finalizer knows the version but runs before the PR exists; a Renovate
`postUpgradeTasks` command knows neither at config-assembly time. So this is
where the build timeline starts.

Why a table instead of an `if` in `upgrade()`
---------------------------------------------
Same reason `helmrelease/state.py` has one: `UpgradeResult.outcome` is a
string with five values, three of which must emit *nothing*, and a printer
already branches on it elsewhere. A table makes "which outcomes are events"
reviewable in one place, and a new outcome fails loudly (absent from the map)
instead of silently emitting nothing.

Why `pr_updated` is not its own phase
-------------------------------------
It was, briefly, and it was wrong. `PR_OPEN` opens the interval for one
`correlation_id` -- one *chart version* -- not for one pull request, and a
re-run against an open PR can legitimately produce a **different** version:
when a major update supersedes a patch, Renovate rebases the branch and the
finalizer retargets from `baseline+patch` to `major+1.0.0`. That run's
outcome is `pr_updated`, so a distinct `pr_updated` phase would leave the
newly-targeted version -- the one that actually gets published -- with no
opening event, and DESIGN.md's duration uncomputable for exactly the
versions that ship.

Emitting `PR_OPEN` for both outcomes keeps the invariant that every version's
timeline starts with `PR_OPEN`. The pull-request-level distinction is
preserved in `detail["outcome"]`, where it is a property of the run rather
than a claim about the version's state.

What is emitted is a transition, not an invocation
--------------------------------------------------
`chart-manager upgrade` is idempotent and operators re-run it freely, so the
emitter is keyed on whether the run **changed the proposal**: it compares the
version already on the open branch -- captured before Renovate runs -- with
the one on it afterwards. Equal means this run proposed nothing new, and
nothing is written.

Without that comparison every invocation appended another identical row --
same correlation_id, same pull request, same version, differing only in uuid
and timestamp -- growing without bound for as long as the pull request stayed
open. A lifecycle stream records state transitions; "someone ran the command
again" is not one.
"""

from __future__ import annotations

from dataclasses import dataclass

from chart_manager.services.events.failure import emit_non_fatal
from chart_manager.services.events.lifecycle import BuildPhase
from chart_manager.services.events.writer import EventWriter

from .models import UpgradeResult

__all__ = ["OUTCOME_PHASE", "UpgradeTelemetry"]


# The outcomes that record a build-lifecycle transition. Both proposal
# outcomes open the interval for the version they propose (see the module
# docstring). Outcomes deliberately absent -- and therefore silent:
#   dry_run        nothing was pushed; there is no artifact to report.
#   no_changes     Renovate found nothing; a non-event.
#   status_unknown the GitHub lookup failed, so there is no trustworthy pull
#                  request behind the proposal. The version read-back is also
#                  unreliable here, and a wrong opening event is worse than a
#                  gap the diagnostics already explain.
OUTCOME_PHASE: dict[str, BuildPhase] = {
    "pr_open": BuildPhase.PR_OPEN,
    "pr_updated": BuildPhase.PR_OPEN,
}


@dataclass(frozen=True)
class UpgradeTelemetry:
    """Emits the build-lifecycle event for one completed upgrade run."""

    writer: EventWriter
    strict: bool = False

    def completed(
        self, result: UpgradeResult, *, previously_proposed: str | None = None
    ) -> None:
        """Emit the phase for `result`, or nothing if it records no transition.

        `previously_proposed` is the wrapper version already on the open
        branch before this run; None when no pull request was open.
        """
        phase = OUTCOME_PHASE.get(result.outcome)
        if phase is None:
            return

        # The proposal is unchanged, so this run transitioned nothing. Emitting
        # here is what made a re-run against an open pull request append an
        # identical row on every invocation.
        if previously_proposed is not None and previously_proposed == result.proposed_version:
            return

        # A missing proposed_version is fatal to the event, not to the run.
        # `correlation_id` is derived as f"{chart}@{version}", so emitting
        # here would write a literal "loki@None" -- an id nothing will ever
        # join to. The version is read back off the pushed branch and is None
        # whenever that read fails or the finalizer callback did not run; the
        # result's own diagnostics already report why.
        if result.proposed_version is None:
            return

        # build_correlation_id is "{repository}#{pr_number}" rather than the PR
        # URL: it is the identifier GitHub Actions can reconstruct from
        # ${{ github.repository }} and ${{ github.event.number }}, so the
        # later validating/merged/published events emitted by CI land on the
        # same build. The URL travels alongside as pr_url.
        build_correlation_id = (
            f"{result.repository}#{result.pr_number}"
            if result.repository and result.pr_number is not None
            else None
        )

        # detail carries only str/int/bool: the DynamoDB adapter hands the item
        # straight to boto3, whose serializer rejects float.
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
                phase=phase,
                build_correlation_id=build_correlation_id,
                pr_url=result.pr_url,
                detail=detail,
            ),
            strict=self.strict,
            what=f"build {phase.value}",
        )
