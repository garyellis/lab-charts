"""Build-lifecycle telemetry emitted by the upgrade service.

`BuildPhase.PR_OPEN` was emitted nowhere: the only `EventWriter.build` caller
was `cli/events.py`, which no workflow invoked, so the build timeline whose
`merged`/`published` phases CI is meant to close had no opening event at all.
This module is the guard on the wiring that opens it -- and, more importantly,
on the three outcomes that must stay *silent*, since a wrong phase on the
timeline is worse than a documented gap.

The `_RecordingEvents` double is local rather than imported from
`test_helmrelease_telemetry.py`: it records `build` calls, not `promote`
calls, and a shared double would couple two suites that assert different
capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chart_manager.services.events.lifecycle import BuildPhase
from chart_manager.services.upgrader.models import UpgradeResult
from chart_manager.services.upgrader.telemetry import OUTCOME_PHASE, UpgradeTelemetry

CHART = "loki"
BASELINE = "1.2.3"
PROPOSED = "1.2.4"
REPOSITORY = "owner/repository"


# ----- doubles -------------------------------------------------------------


@dataclass
class _RecordingEvents:
    """EventWriter stand-in that records every build event it is handed."""

    events: list[dict[str, Any]] = field(default_factory=list)
    raises: BaseException | None = None

    def build(self, **kwargs: Any) -> None:
        self.events.append(kwargs)
        if self.raises is not None:
            raise self.raises

    @property
    def phases(self) -> list[BuildPhase]:
        return [e["phase"] for e in self.events]


def _result(
    outcome: str,
    *,
    proposed_version: str | None = PROPOSED,
    pr_number: int | None = 7,
    repository: str | None = REPOSITORY,
    branch: str | None = "renovate/loki/loki",
) -> UpgradeResult:
    return UpgradeResult(
        chart=CHART,
        chart_path=Path("charts/loki"),
        current_version=BASELINE,
        proposed_version=proposed_version,
        branch=branch,
        group=f"chart-manager:{CHART}",
        outcome=outcome,
        repository=repository,
        pr_url="https://example.test/pull/7" if pr_number is not None else None,
        pr_number=pr_number,
    )


# ----- the mapping ---------------------------------------------------------


def test_pr_open_emits_the_opening_build_event() -> None:
    events = _RecordingEvents()
    UpgradeTelemetry(writer=events).completed(_result("pr_open"))

    assert events.phases == [BuildPhase.PR_OPEN]
    emitted = events.events[0]
    assert emitted["chart_name"] == CHART
    # The *proposed* version, not the baseline: correlation_id is derived from
    # chart@version and must name the artifact the PR proposes.
    assert emitted["chart_version"] == PROPOSED
    assert emitted["pr_url"] == "https://example.test/pull/7"
    assert emitted["detail"]["previous_version"] == BASELINE


def test_pr_updated_also_opens_the_interval_for_the_version_it_proposes() -> None:
    """A re-run against an open PR can propose a *different* version.

    When a major update supersedes a patch, Renovate rebases the branch and
    the finalizer retargets from baseline+patch to major+1.0.0 -- while the
    pull request stays open, so the outcome is `pr_updated`. That new version
    is the one that gets published, and PR_OPEN opens the interval for a
    *version*, not for a pull request. A distinct PR_UPDATED phase would
    leave it with no opening event and its duration uncomputable.
    """
    events = _RecordingEvents()
    UpgradeTelemetry(writer=events).completed(
        _result("pr_updated", proposed_version="2.0.0")
    )

    assert events.phases == [BuildPhase.PR_OPEN]
    assert events.events[0]["chart_version"] == "2.0.0"
    # The run-level distinction survives where it belongs: as a property of
    # the run, not as a claim about the version's state.
    assert events.events[0]["detail"]["outcome"] == "pr_updated"


def test_every_emitted_build_event_opens_the_interval() -> None:
    """The invariant: a version's build timeline always starts with PR_OPEN."""
    assert set(OUTCOME_PHASE.values()) == {BuildPhase.PR_OPEN}


# ----- transitions, not invocations ---------------------------------------


def test_a_rerun_that_changes_nothing_emits_nothing() -> None:
    """`chart-manager upgrade` is idempotent; re-running is not an event.

    Observed against a real account: three runs on one open pull request wrote
    three identical rows -- same correlation_id, same PR, same version --
    differing only in uuid and timestamp. Left alone this grows without bound
    for as long as the pull request stays open.
    """
    events = _RecordingEvents()
    UpgradeTelemetry(writer=events).completed(
        _result("pr_updated", proposed_version="0.1.1"), previously_proposed="0.1.1"
    )

    assert events.events == []


def test_a_rerun_that_retargets_the_version_still_emits() -> None:
    """The escalation case must survive the de-duplication."""
    events = _RecordingEvents()
    UpgradeTelemetry(writer=events).completed(
        _result("pr_updated", proposed_version="2.0.0"), previously_proposed="1.2.4"
    )

    assert events.phases == [BuildPhase.PR_OPEN]
    assert events.events[0]["chart_version"] == "2.0.0"


def test_the_run_that_opens_the_pull_request_always_emits() -> None:
    """No branch existed beforehand, so there is nothing it could duplicate."""
    events = _RecordingEvents()
    UpgradeTelemetry(writer=events).completed(
        _result("pr_open", proposed_version="0.1.1"), previously_proposed=None
    )

    assert events.phases == [BuildPhase.PR_OPEN]


def test_the_observed_crossplane_sequence_writes_exactly_one_row() -> None:
    """Reproduces the reported defect: open, then two no-op re-runs."""
    events = _RecordingEvents()
    telemetry = UpgradeTelemetry(writer=events)

    telemetry.completed(_result("pr_open", proposed_version="0.1.1"), previously_proposed=None)
    telemetry.completed(
        _result("pr_updated", proposed_version="0.1.1"), previously_proposed="0.1.1"
    )
    telemetry.completed(
        _result("pr_updated", proposed_version="0.1.1"), previously_proposed="0.1.1"
    )

    assert len(events.events) == 1
    assert events.events[0]["detail"]["outcome"] == "pr_open"


@pytest.mark.parametrize("outcome", ["dry_run", "no_changes", "status_unknown"])
def test_outcomes_without_a_transition_emit_nothing(outcome: str) -> None:
    """dry_run pushed nothing, no_changes found nothing, status_unknown knows nothing."""
    events = _RecordingEvents()
    UpgradeTelemetry(writer=events).completed(_result(outcome))

    assert events.events == []


def test_outcome_map_covers_only_the_two_pull_request_outcomes() -> None:
    """A new outcome must be added deliberately, not inherit a phase by accident."""
    assert set(OUTCOME_PHASE) == {"pr_open", "pr_updated"}


# ----- the poisoned-partition guard ---------------------------------------


def test_missing_proposed_version_emits_nothing() -> None:
    """`chart@None` would be a correlation id nothing can ever join to.

    The version is read back off the pushed branch, so it is None whenever
    that read fails or the upgrade-finalize callback did not run -- both of
    which can coexist with an open PR.
    """
    events = _RecordingEvents()
    UpgradeTelemetry(writer=events).completed(
        _result("pr_open", proposed_version=None)
    )

    assert events.events == []


# ----- correlation --------------------------------------------------------


def test_build_correlation_id_is_repository_and_pr_number() -> None:
    """CI must be able to reconstruct this from the GitHub Actions context."""
    events = _RecordingEvents()
    UpgradeTelemetry(writer=events).completed(_result("pr_open"))

    assert events.events[0]["build_correlation_id"] == "owner/repository#7"


@pytest.mark.parametrize(
    ("repository", "pr_number"),
    [(None, 7), (REPOSITORY, None)],
)
def test_build_correlation_id_is_omitted_when_either_half_is_missing(
    repository: str | None, pr_number: int | None
) -> None:
    """Half an identifier is worse than none: it would not join to anything."""
    events = _RecordingEvents()
    UpgradeTelemetry(writer=events).completed(
        _result("pr_open", repository=repository, pr_number=pr_number)
    )

    assert events.events[0]["build_correlation_id"] is None


def test_detail_carries_only_dynamodb_safe_scalars() -> None:
    """The DynamoDB adapter hands detail to boto3, whose serializer rejects float."""
    events = _RecordingEvents()
    UpgradeTelemetry(writer=events).completed(_result("pr_open"))

    detail = events.events[0]["detail"]
    assert detail
    assert all(isinstance(value, (str, int, bool)) for value in detail.values())


# ----- the failure policy -------------------------------------------------


def test_emission_failure_is_swallowed_by_default() -> None:
    """The upgrade is already pushed; a dropped event must not fail the run."""
    events = _RecordingEvents(raises=RuntimeError("cosmos unreachable"))

    UpgradeTelemetry(writer=events).completed(_result("pr_open"))


def test_strict_reraises_for_callers_where_the_event_is_the_deliverable() -> None:
    events = _RecordingEvents(raises=RuntimeError("cosmos unreachable"))

    with pytest.raises(RuntimeError):
        UpgradeTelemetry(writer=events, strict=True).completed(_result("pr_open"))
