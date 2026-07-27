"""Read-only live observers for cluster-backed lifecycle actions.

Observers translate facts from injected Helm and Kubernetes query ports into
ephemeral ``source=live`` evidence.  They never persist evidence, mutate a
cluster, or infer success when the requested object cannot be observed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import uuid4

from chart_manager.services.lifecycle.evidence import (
    ClusterIdentity,
    EvidenceRecord,
    EvidenceStatus,
    EvidenceVerdict,
    TargetCoordinates,
)
from chart_manager.services.lifecycle.models import ActionKind, LifecycleAction


class HelmReleaseSnapshot(Protocol):
    """The stable subset of one ``helm list -o json`` row."""

    name: str
    namespace: str
    revision: int
    status: str


class HelmReleaseReader(Protocol):
    """Port implemented by the existing Helm adapter."""

    def list_releases(
        self,
        *,
        all_namespaces: bool = True,
        namespace: str | None = None,
    ) -> Sequence[HelmReleaseSnapshot]:
        """Return a current Helm release-list snapshot."""


class KubernetesSnapshotReader(Protocol):
    """Read-only JSON query subset implemented by the existing Kubectl adapter."""

    def get_json(
        self,
        args: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run a read-only Kubernetes query and decode its JSON object."""


def _now() -> datetime:
    return datetime.now(UTC)


def _run_id() -> str:
    return f"live-{uuid4()}"


def _target(action: LifecycleAction) -> TargetCoordinates:
    target = action.target
    return TargetCoordinates(
        workflow=str(target.workflow),
        chart=target.chart,
        profile=target.profile,
        environment=target.environment,
        release=target.release,
        namespace=target.namespace,
    )


def _record(
    action: LifecycleAction,
    *,
    cluster: ClusterIdentity,
    run_id: str,
    observed_at: datetime,
    verdict: EvidenceVerdict,
    status: EvidenceStatus,
    reason: str,
    detail: str,
) -> EvidenceRecord:
    return EvidenceRecord(
        run_id=run_id,
        action_id=action.action_id,
        action_kind=str(action.kind),
        target=_target(action),
        verdict=verdict,
        status=status,
        reason=reason,
        detail=detail,
        input_digest=action.input_digest,
        source="live",
        cluster=cluster,
        started_at=observed_at,
        finished_at=observed_at,
        recorded_at=observed_at,
    )


def _unknown_record(
    action: LifecycleAction,
    *,
    cluster: ClusterIdentity,
    run_id: str,
    observed_at: datetime,
    status: EvidenceStatus,
    reason: str,
    detail: str,
) -> EvidenceRecord:
    """Record an observed absence without allowing cached success to win."""

    return _record(
        action,
        cluster=cluster,
        run_id=run_id,
        observed_at=observed_at,
        verdict="UNKNOWN",
        status=status,
        reason=reason,
        detail=detail,
    )


class HelmReleaseStatusObserver:
    """Observe install actions from one lazily acquired Helm list snapshot."""

    def __init__(
        self,
        helm: HelmReleaseReader,
        *,
        cluster: ClusterIdentity,
        run_id: str | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._helm = helm
        self._cluster = cluster
        self._run_id = run_id or _run_id()
        self._clock = clock
        self._snapshot: tuple[HelmReleaseSnapshot, ...] | None = None

    def observe(self, action: LifecycleAction) -> EvidenceRecord | None:
        """Return current release evidence for an install action, if it exists."""

        if action.kind is not ActionKind.HELM_UPGRADE_INSTALL:
            return None
        release_name = action.target.release
        namespace = action.target.namespace
        if not release_name or not namespace:
            return None

        if self._snapshot is None:
            self._snapshot = tuple(self._helm.list_releases(all_namespaces=True))
        release = next(
            (
                candidate
                for candidate in self._snapshot
                if candidate.name == release_name and candidate.namespace == namespace
            ),
            None,
        )
        if release is None:
            return _unknown_record(
                action,
                cluster=self._cluster,
                run_id=self._run_id,
                observed_at=self._clock(),
                status="missing",
                reason="ReleaseNotFound",
                detail=f"release={release_name} namespace={namespace} was not observed",
            )

        release_status = release.status.casefold()
        deployed = release_status == "deployed"
        known_statuses = {
            "deployed",
            "failed",
            "uninstalled",
            "superseded",
            "uninstalling",
            "pending-install",
            "pending-upgrade",
            "pending-rollback",
        }
        if release_status not in known_statuses:
            return _unknown_record(
                action,
                cluster=self._cluster,
                run_id=self._run_id,
                observed_at=self._clock(),
                status="unknown",
                reason="ReleaseStatusUnknown",
                detail=(
                    f"release={release.name} namespace={release.namespace} "
                    f"revision={release.revision} status={release.status}"
                ),
            )
        return _record(
            action,
            cluster=self._cluster,
            run_id=self._run_id,
            observed_at=self._clock(),
            verdict="PASS" if deployed else "FAIL",
            status=cast(EvidenceStatus, release_status),
            reason="ReleaseDeployed" if deployed else "ReleaseNotDeployed",
            detail=(
                f"release={release.name} namespace={release.namespace} "
                f"revision={release.revision} status={release.status}"
            ),
        )


class WorkloadReadinessObserver:
    """Observe readiness for workloads targeted by a workload-ready action."""

    _RESOURCE_QUERY = "deployment,statefulset,daemonset"

    def __init__(
        self,
        kubectl: KubernetesSnapshotReader,
        *,
        cluster: ClusterIdentity,
        run_id: str | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._kubectl = kubectl
        self._cluster = cluster
        self._run_id = run_id or _run_id()
        self._clock = clock

    def observe(self, action: LifecycleAction) -> EvidenceRecord | None:
        """Return readiness evidence from one Kubernetes list response."""

        if action.kind is not ActionKind.WORKLOAD_READY:
            return None
        namespace = action.target.namespace
        if not namespace:
            return None
        payload = self._kubectl.get_json(
            ["-n", namespace, "get", self._RESOURCE_QUERY, "-o", "json"]
        )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            return _unknown_record(
                action,
                cluster=self._cluster,
                run_id=self._run_id,
                observed_at=self._clock(),
                status="unobservable",
                reason="WorkloadsUnobservable",
                detail=f"namespace={namespace} workload response did not contain an item list",
            )

        workloads = [
            snapshot
            for item in raw_items
            if _belongs_to_release(item, action.target.release, namespace)
            and (snapshot := _workload_snapshot(item)) is not None
        ]
        observed_at = self._clock()
        if not workloads:
            return _unknown_record(
                action,
                cluster=self._cluster,
                run_id=self._run_id,
                observed_at=observed_at,
                status="missing",
                reason="OwnedWorkloadsNotFound",
                detail=(
                    f"release={action.target.release or '-'} namespace={namespace} "
                    "ownedWorkloads=0"
                ),
            )

        pending = [snapshot for snapshot in workloads if not snapshot[1]]
        detail = "; ".join(snapshot[0] for snapshot in workloads)
        return _record(
            action,
            cluster=self._cluster,
            run_id=self._run_id,
            observed_at=observed_at,
            verdict="PASS" if not pending else "FAIL",
            status="ready" if not pending else "not-ready",
            reason="WorkloadsReady" if not pending else "WorkloadsNotReady",
            detail=detail,
        )


def _integer(value: object, default: int = 0) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return default
    try:
        return int(value)
    except (OverflowError, TypeError, ValueError):
        return default


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _belongs_to_release(
    item: object,
    release: str | None,
    namespace: str,
) -> bool:
    """Whether Kubernetes ownership metadata ties ``item`` to this Helm release."""

    if not release or not isinstance(item, Mapping):
        return False
    metadata = _mapping(item.get("metadata"))
    labels = _mapping(metadata.get("labels"))
    if labels.get("app.kubernetes.io/instance") == release:
        return True
    annotations = _mapping(metadata.get("annotations"))
    release_namespace = annotations.get("meta.helm.sh/release-namespace")
    return (
        annotations.get("meta.helm.sh/release-name") == release
        and (release_namespace is None or release_namespace == namespace)
    )


def _workload_snapshot(item: object) -> tuple[str, bool] | None:
    """Return a compact description and convergence fact for one workload."""

    if not isinstance(item, Mapping):
        return None
    kind = str(item.get("kind") or "")
    metadata = _mapping(item.get("metadata"))
    spec = _mapping(item.get("spec"))
    status = _mapping(item.get("status"))
    name = str(metadata.get("name") or "")
    if not name:
        return None

    if kind == "Deployment":
        desired = _integer(spec.get("replicas"), 1)
        ready = _integer(status.get("readyReplicas"))
        available = _integer(status.get("availableReplicas"))
    elif kind == "StatefulSet":
        desired = _integer(spec.get("replicas"), 1)
        ready = _integer(status.get("readyReplicas"))
        available = _integer(status.get("availableReplicas"), ready)
    elif kind == "DaemonSet":
        desired = _integer(status.get("desiredNumberScheduled"))
        ready = _integer(status.get("numberReady"))
        available = _integer(status.get("numberAvailable"))
    else:
        return None

    generation = _integer(metadata.get("generation"))
    observed_generation = _integer(status.get("observedGeneration"))
    converged = (
        desired >= 0
        and observed_generation >= generation
        and ready == desired
        and available == desired
    )
    description = (
        f"{kind}/{name} ready={ready}/{desired} available={available}/{desired} "
        f"generation={observed_generation}/{generation}"
    )
    return description, converged
