"""Freshness-aware lifecycle status projected from plans and evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from chart_manager.services.lifecycle.evidence import (
    ClusterIdentity,
    EvidenceDiagnostic,
    EvidenceHistory,
    EvidenceRecord,
    LocalEvidenceRepository,
    TargetCoordinates,
)

Freshness = Literal["current", "stale", "unknown"]
StatusOrigin = Literal["cached", "live"]
STATUS_API_VERSION = "lifecycle.cmg.io/v1alpha1"


class ActionTargetLike(Protocol):
    """Coordinates needed from a compiled action target."""

    chart: str
    profile: str | None


class LifecycleActionLike(Protocol):
    """Structural seam between the compiler and status projection."""

    action_id: str
    kind: str
    target: ActionTargetLike
    input_digest: str


class LifecyclePlanLike(Protocol):
    """Minimal compiled-plan surface required by status."""

    workflow: object
    actions: Sequence[LifecycleActionLike]


class StatusObserver(Protocol):
    """Optional live observer seam; observers must return facts, not guesses."""

    def observe(self, action: LifecycleActionLike) -> EvidenceRecord | None:
        """Observe one action target, or return ``None`` when it cannot be observed."""


@dataclass(frozen=True)
class EvidenceSourceMetadata:
    """Where and when the evidence used for a status row was obtained."""

    origin: StatusOrigin
    source: str
    observed_at: datetime
    evidence_id: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["observedAt"] = self.observed_at.isoformat()
        data.pop("observed_at")
        data["evidenceId"] = data.pop("evidence_id")
        return data


@dataclass(frozen=True)
class ActionStatus:
    """Projected condition for one action in the currently compiled plan."""

    action_id: str
    kind: str
    target: TargetCoordinates
    expected_input_digest: str
    freshness: Freshness
    verdict: str | None = None
    status: str | None = None
    reason: str | None = None
    detail: str | None = None
    source: EvidenceSourceMetadata | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["actionId"] = data.pop("action_id")
        data["expectedInputDigest"] = data.pop("expected_input_digest")
        if self.source is not None:
            data["source"] = self.source.to_dict()
        return data


@dataclass(frozen=True)
class StatusCondition:
    """A summary condition for an action kind, without collapsing to a boolean."""

    type: str
    status: Literal["PASS", "FAIL", "STALE", "UNKNOWN", "MIXED"]
    reason: str
    current: int
    stale: int
    unknown: int
    failed: int
    skipped: int
    total: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LifecycleStatus:
    """Full status projection for a compiled lifecycle plan."""

    actions: tuple[ActionStatus, ...]
    conditions: tuple[StatusCondition, ...]
    diagnostics: tuple[EvidenceDiagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "apiVersion": STATUS_API_VERSION,
            "kind": "LifecycleStatus",
            "actions": [action.to_dict() for action in self.actions],
            "conditions": [condition.to_dict() for condition in self.conditions],
            "diagnostics": [
                {"path": str(diagnostic.path), "message": diagnostic.message}
                for diagnostic in self.diagnostics
            ],
        }


def _coordinate(target: ActionTargetLike, name: str) -> str | None:
    value = getattr(target, name, None)
    return value if isinstance(value, str) else None


def _target_coordinates(target: ActionTargetLike, workflow: object) -> TargetCoordinates:
    target_workflow = getattr(target, "workflow", workflow)
    return TargetCoordinates(
        chart=target.chart,
        workflow=str(target_workflow),
        profile=_coordinate(target, "profile"),
        environment=_coordinate(target, "environment"),
        release=_coordinate(target, "release"),
        namespace=_coordinate(target, "namespace"),
    )


def _latest(records: Iterable[tuple[EvidenceRecord, StatusOrigin]]) -> tuple[
    EvidenceRecord, StatusOrigin
] | None:
    materialized = list(records)
    if not materialized:
        return None
    # At equal timestamps prefer a live observation supplied for this projection.
    return max(
        materialized,
        key=lambda pair: (
            pair[0].recorded_at,
            pair[0].finished_at,
            pair[1] == "live",
            pair[0].evidence_id,
        ),
    )


def _cluster_matches(
    evidence_cluster: ClusterIdentity | None,
    requested_cluster: ClusterIdentity,
) -> bool:
    """Match every cluster coordinate the caller was able to resolve."""

    if evidence_cluster is None or evidence_cluster.name != requested_cluster.name:
        return False
    return all(
        expected is None or actual == expected
        for actual, expected in (
            (evidence_cluster.context, requested_cluster.context),
            (evidence_cluster.uid, requested_cluster.uid),
            (
                evidence_cluster.kubernetes_version,
                requested_cluster.kubernetes_version,
            ),
        )
    )


def _select(
    candidates: Sequence[tuple[EvidenceRecord, StatusOrigin]],
    *,
    input_digest: str | None = None,
) -> tuple[EvidenceRecord, StatusOrigin] | None:
    """Prefer evidence observed during this invocation over any cached record."""

    matching = [
        pair
        for pair in candidates
        if input_digest is None or pair[0].input_digest == input_digest
    ]
    live = _latest(pair for pair in matching if pair[1] == "live")
    return live or _latest(pair for pair in matching if pair[1] == "cached")


def _condition(kind: str, rows: Sequence[ActionStatus]) -> StatusCondition:
    current = sum(row.freshness == "current" for row in rows)
    stale = sum(row.freshness == "stale" for row in rows)
    unknown = sum(
        row.freshness == "unknown"
        or (
            row.freshness == "current"
            and row.verdict not in {"PASS", "FAIL", "SKIP"}
        )
        for row in rows
    )
    failed = sum(
        row.freshness == "current" and row.verdict == "FAIL"
        for row in rows
    )
    skipped = sum(
        row.freshness == "current" and row.verdict == "SKIP"
        for row in rows
    )
    passed = sum(
        row.freshness == "current" and row.verdict == "PASS" for row in rows
    )
    if failed:
        status: Literal["PASS", "FAIL", "STALE", "UNKNOWN", "MIXED"] = "FAIL"
        reason = "CurrentEvidenceFailed"
    elif unknown == len(rows):
        status = "UNKNOWN"
        reason = "NoEvidence"
    elif stale and not current and not unknown:
        status = "STALE"
        reason = "InputDigestChanged"
    elif skipped == len(rows):
        status = "UNKNOWN"
        reason = "ActionsSkipped"
    elif unknown or stale or skipped:
        status = "MIXED"
        reason = "IncompleteStaleOrSkippedEvidence"
    elif passed == len(rows):
        status = "PASS"
        reason = "CurrentEvidencePassed"
    else:
        status = "UNKNOWN"
        reason = "EvidenceVerdictUnknown"
    return StatusCondition(
        type=kind,
        status=status,
        reason=reason,
        current=current,
        stale=stale,
        unknown=unknown,
        failed=failed,
        skipped=skipped,
        total=len(rows),
    )


def project_status(
    plan: LifecyclePlanLike,
    cached_evidence: EvidenceHistory | Iterable[EvidenceRecord],
    *,
    live_observations: Iterable[EvidenceRecord] = (),
    requested_cluster: ClusterIdentity | None = None,
) -> LifecycleStatus:
    """Project action and summary conditions from cached and live evidence.

    ``live_observations`` are intentionally supplied separately.  Merely loading
    an old record whose source says ``live`` does not make it a live observation
    in the current invocation.
    """

    if isinstance(cached_evidence, EvidenceHistory):
        cached_records = cached_evidence.records
        diagnostics = cached_evidence.diagnostics
    else:
        cached_records = tuple(cached_evidence)
        diagnostics = ()
    live_records = tuple(live_observations)
    if requested_cluster is None:
        observed_clusters = {
            record.cluster for record in live_records if record.cluster is not None
        }
        if len(observed_clusters) == 1:
            requested_cluster = next(iter(observed_clusters))

    by_action: dict[str, list[tuple[EvidenceRecord, StatusOrigin]]] = defaultdict(list)
    for record in cached_records:
        if (
            requested_cluster is not None
            and record.target.workflow == "cluster-test"
            and not _cluster_matches(record.cluster, requested_cluster)
        ):
            continue
        by_action[record.action_id].append((record, "cached"))
    for record in live_records:
        by_action[record.action_id].append((record, "live"))

    statuses: list[ActionStatus] = []
    for action in plan.actions:
        target = _target_coordinates(action.target, plan.workflow)
        candidates = [
            pair for pair in by_action.get(action.action_id, ()) if pair[0].target == target
        ]
        current = _select(candidates, input_digest=action.input_digest)
        selected = current or _select(candidates)
        if selected is None:
            statuses.append(
                ActionStatus(
                    action_id=action.action_id,
                    kind=str(action.kind),
                    target=target,
                    expected_input_digest=action.input_digest,
                    freshness="unknown",
                )
            )
            continue
        record, origin = selected
        statuses.append(
            ActionStatus(
                action_id=action.action_id,
                kind=str(action.kind),
                target=target,
                expected_input_digest=action.input_digest,
                freshness="current" if current is not None else "stale",
                verdict=record.verdict,
                status=record.status,
                reason=record.reason,
                detail=record.detail,
                source=EvidenceSourceMetadata(
                    origin=origin,
                    source=record.source,
                    observed_at=record.recorded_at,
                    evidence_id=record.evidence_id,
                ),
            )
        )

    rows_by_kind: dict[str, list[ActionStatus]] = defaultdict(list)
    for status in statuses:
        rows_by_kind[status.kind].append(status)
    conditions = tuple(
        _condition(kind, rows_by_kind[kind]) for kind in sorted(rows_by_kind)
    )
    return LifecycleStatus(tuple(statuses), conditions, diagnostics)


class LifecycleStatusService:
    """Application service that projects repository evidence over a plan."""

    def __init__(self, repository: LocalEvidenceRepository) -> None:
        self.repository = repository

    def project(
        self,
        plan: LifecyclePlanLike,
        *,
        observers: Iterable[StatusObserver] = (),
        requested_cluster: ClusterIdentity | None = None,
    ) -> LifecycleStatus:
        """Read history, make requested live observations, and project status."""

        observations: list[EvidenceRecord] = []
        for action in plan.actions:
            for observer in observers:
                observation = observer.observe(action)
                if observation is not None:
                    observations.append(observation)
        return project_status(
            plan,
            self.repository.history(),
            live_observations=observations,
            requested_cluster=requested_cluster,
        )
