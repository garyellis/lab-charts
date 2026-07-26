"""Client for reading Flux HelmRelease state, built on the kubectl adapter.

All methods are read-only, stateless, and safe under Python-thread
concurrency; the caller owns kubeconfig/context and any external
rate-limiting (recommended bound ~8 concurrent calls per kubeconfig due
to exec-auth-plugin token-cache races on EKS/GKE). No retries, no waits
-- the service layer owns budgets.

Nothing here invokes the `flux` binary: HelmReleases are ordinary custom
resources, so every query is a `kubectl get`. That is why this composes
`Kubectl` rather than holding its own `CommandRunner` -- while it did, the
codebase had two adapters wrapping the same CLI with different context,
timeout and JSON-parse conventions.
"""
from __future__ import annotations

import builtins
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from chart_manager.integrations.kubectl import Kubectl

_FLUX_GROUP_PREFIX = "helm.toolkit.fluxcd.io/"


@dataclass(frozen=True)
class HelmReleaseRef:
    """Identity of one HelmRelease plus its derived helm release name/namespaces."""

    name: str
    namespace: str
    api_version: str
    release_name: str
    storage_namespace: str
    target_namespace: str


@dataclass(frozen=True)
class ConditionSnapshot:
    """One entry from `status.conditions`, normalized to strings + parsed timestamp."""

    type: str
    status: str
    reason: str
    message: str
    last_transition_time: datetime | None


@dataclass(frozen=True)
class HelmReleaseStatus:
    """Point-in-time snapshot of a HelmRelease's spec/status fields we care about."""

    ref: HelmReleaseRef
    observed_at: datetime
    generation: int
    observed_generation: int
    resource_version: str
    suspended: bool
    desired_chart_name: str | None
    desired_chart_version: str | None
    last_applied_revision: str | None
    history_chart_version: str | None
    conditions: tuple[ConditionSnapshot, ...]

    def condition(self, type_: str) -> ConditionSnapshot | None:
        """Return the first condition of the given type, or None."""
        for cond in self.conditions:
            if cond.type == type_:
                return cond
        return None

    @property
    def ready(self) -> ConditionSnapshot | None:
        """The Ready condition, or None."""
        return self.condition("Ready")

    @property
    def released(self) -> ConditionSnapshot | None:
        """The Released condition, or None."""
        return self.condition("Released")

    @property
    def test_success(self) -> ConditionSnapshot | None:
        """The TestSuccess condition, or None."""
        return self.condition("TestSuccess")


@dataclass(frozen=True)
class OwnedWorkload:
    """Replica counts for one Deployment/StatefulSet/DaemonSet owned by a release."""

    kind: str
    namespace: str
    name: str
    desired: int
    ready: int
    available: int


@dataclass(frozen=True)
class WorkloadRollout:
    """An OwnedWorkload plus a converged verdict (generation observed, all replicas up)."""

    workload: OwnedWorkload
    converged: bool
    generation: int
    observed_generation: int


class HelmReleaseClient:
    """Read-only queries against Flux HelmReleases and their owned workloads.

    Narrow by design: only the four queries that encode HelmRelease shape
    live here. Generic pod and event operations belong to `Kubectl`, which
    is also what addresses the cluster for this client.
    """

    def __init__(self, kubectl: Kubectl | None = None) -> None:
        """Bind the kubectl adapter that addresses the target cluster."""
        self._kubectl = kubectl if kubectl is not None else Kubectl()

    def list(
        self,
        *,
        namespace: str | None = None,
        timeout: float | None = None,
    ) -> builtins.list[HelmReleaseRef]:
        """List HelmReleases (all namespaces by default); unparseable items are skipped."""
        args = ["kubectl", "get", "helmreleases.helm.toolkit.fluxcd.io"]
        if namespace is None:
            args.append("-A")
        else:
            args.extend(["-n", namespace])
        args.extend(["-o", "json"])
        payload = self._kubectl.get_json(args, timeout=timeout)
        refs: builtins.list[HelmReleaseRef] = []
        for item in payload.get("items", []) or []:
            ref = _ref_from_item(item)
            if ref is not None:
                refs.append(ref)
        return refs

    def get_status(
        self,
        ref: HelmReleaseRef,
        *,
        timeout: float | None = None,
    ) -> HelmReleaseStatus:
        """Fetch one HelmRelease and snapshot its status (stamped with wall-clock time)."""
        args = [
            "kubectl", "-n", ref.namespace, "get",
            "helmreleases.helm.toolkit.fluxcd.io", ref.name, "-o", "json",
        ]
        payload = self._kubectl.get_json(args, timeout=timeout)
        observed_at = datetime.now(UTC)
        return _status_from_item(payload, ref, observed_at)

    def list_owned_workloads(
        self,
        ref: HelmReleaseRef,
        *,
        timeout: float | None = None,
    ) -> builtins.list[WorkloadRollout]:
        """List workloads labeled as owned by this release, with rollout convergence."""
        selector = (
            f"helm.toolkit.fluxcd.io/name={ref.name},"
            f"helm.toolkit.fluxcd.io/namespace={ref.namespace}"
        )
        args = [
            "kubectl", "get", "deployment,statefulset,daemonset",
            "-A", "-l", selector, "-o", "json",
        ]
        payload = self._kubectl.get_json(args, timeout=timeout)
        rollouts: builtins.list[WorkloadRollout] = []
        for item in payload.get("items", []) or []:
            rollout = _rollout_from_item(item)
            if rollout is not None:
                rollouts.append(rollout)
        return rollouts

    def list_test_pods(
        self,
        ref: HelmReleaseRef,
        *,
        timeout: float | None = None,
    ) -> builtins.list[tuple[str, str, str]]:
        """Return (namespace, name, phase) for this release's helm test hook pods.

        Queries the target namespace for both `helm.sh/hook=test` and the
        legacy `test-success` label, deduping pods that carry both.
        """
        base = (
            f"helm.toolkit.fluxcd.io/name={ref.name},"
            f"helm.toolkit.fluxcd.io/namespace={ref.namespace}"
        )
        seen: set[tuple[str, str]] = set()
        pods: builtins.list[tuple[str, str, str]] = []
        for hook in ("test", "test-success"):
            args = [
                "kubectl", "-n", ref.target_namespace, "get", "pods",
                "-l", f"{base},helm.sh/hook={hook}", "-o", "json",
            ]
            payload = self._kubectl.get_json(args, timeout=timeout)
            for item in payload.get("items", []) or []:
                metadata = item.get("metadata") or {}
                ns = str(metadata.get("namespace") or "")
                name = str(metadata.get("name") or "")
                if not name:
                    continue
                key = (ns, name)
                if key in seen:
                    continue
                seen.add(key)
                phase = str((item.get("status") or {}).get("phase") or "")
                pods.append((ns, name, phase))
        return pods


def _ref_from_item(item: Any) -> HelmReleaseRef | None:
    """Build a HelmReleaseRef from a raw item, deriving the helm release name.

    Returns None for non-Flux or malformed items. Encodes the helm-controller
    naming rule (releaseName > targetNamespace-name > name).
    """
    if not isinstance(item, dict):
        return None
    api_version = item.get("apiVersion", "")
    # Match by group prefix so v2beta1/v2beta2/v2 all flow through one path.
    if not (isinstance(api_version, str) and api_version.startswith(_FLUX_GROUP_PREFIX)):
        return None
    metadata = _dict(item.get("metadata"))
    name = str(metadata.get("name") or "")
    namespace = str(metadata.get("namespace") or "")
    if not name or not namespace:
        return None
    spec = _dict(item.get("spec"))
    spec_release_name = spec.get("releaseName")
    target_ns_raw = spec.get("targetNamespace")
    target_ns = str(target_ns_raw) if target_ns_raw else None
    # Flux helm-controller release name rule:
    #   spec.releaseName if set, else "<targetNamespace>-<metadata.name>"
    #   when targetNamespace is set (even if it equals metadata.namespace),
    #   else metadata.name. Empty string "" on either field is treated as
    #   unset to match how the controller's truthy check behaves.
    if spec_release_name:
        release_name = str(spec_release_name)
    elif target_ns:
        release_name = f"{target_ns}-{name}"
    else:
        release_name = name
    target_namespace = target_ns if target_ns else namespace
    storage_namespace = str(
        spec.get("storageNamespace")
        or spec.get("targetNamespace")
        or namespace
    )
    return HelmReleaseRef(
        name=name,
        namespace=namespace,
        api_version=api_version,
        release_name=release_name,
        storage_namespace=storage_namespace,
        target_namespace=target_namespace,
    )


def _status_from_item(
    payload: dict[str, Any],
    ref: HelmReleaseRef,
    observed_at: datetime,
) -> HelmReleaseStatus:
    """Extract the fields we track from a HelmRelease object into a status snapshot."""
    metadata = _dict(payload.get("metadata"))
    spec = _dict(payload.get("spec"))
    status = _dict(payload.get("status"))

    chart_spec: dict[str, Any] = {}
    spec_chart = _dict(spec.get("chart"))
    chart_spec = _dict(spec_chart.get("spec"))

    history = status.get("history") if isinstance(status.get("history"), list) else []
    history_chart_version: str | None = None
    # status.history is newest-first; [0] is the latest release attempt.
    if history and isinstance(history[0], dict):
        raw = history[0].get("chartVersion")
        history_chart_version = str(raw) if raw is not None else None

    conditions = tuple(
        _condition_from_item(c)
        for c in (status.get("conditions") or [])
        if isinstance(c, dict)
    )

    return HelmReleaseStatus(
        ref=ref,
        observed_at=observed_at,
        generation=int(metadata.get("generation") or 0),
        # -1 sentinel = controller has not observed any generation yet.
        observed_generation=int(status.get("observedGeneration", -1)),
        resource_version=str(metadata.get("resourceVersion") or ""),
        suspended=bool(spec.get("suspend")),
        desired_chart_name=_opt_str(chart_spec.get("chart")),
        desired_chart_version=_opt_str(chart_spec.get("version")),
        last_applied_revision=_opt_str(status.get("lastAppliedRevision")),
        history_chart_version=history_chart_version,
        conditions=conditions,
    )


def _condition_from_item(item: dict[str, Any]) -> ConditionSnapshot:
    """Normalize one raw condition dict into a ConditionSnapshot."""
    return ConditionSnapshot(
        type=str(item.get("type") or ""),
        status=str(item.get("status") or ""),
        reason=str(item.get("reason") or ""),
        message=str(item.get("message") or ""),
        last_transition_time=_parse_iso8601(item.get("lastTransitionTime")),
    )


def _parse_iso8601(value: Any) -> datetime | None:
    """Parse a k8s timestamp to aware-UTC datetime; None if missing/unparseable."""
    if not isinstance(value, str) or not value:
        return None
    # Python <3.11 fromisoformat rejects a trailing "Z"; rewrite it to +00:00.
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _rollout_from_item(item: Any) -> WorkloadRollout | None:
    """Map a workload object to a WorkloadRollout; None for unsupported kinds.

    Converged = controller observed the current generation AND ready/available
    both equal desired. Replica fields differ per kind (DaemonSets have no
    spec.replicas; StatefulSets may omit availableReplicas, so ready is the
    fallback).
    """
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "")
    metadata = _dict(item.get("metadata"))
    namespace = str(metadata.get("namespace") or "")
    name = str(metadata.get("name") or "")
    if not name or not namespace:
        return None
    spec = _dict(item.get("spec"))
    status = _dict(item.get("status"))

    if kind == "Deployment":
        desired = int(spec.get("replicas", 1))
        ready = int(status.get("readyReplicas") or 0)
        available = int(status.get("availableReplicas") or 0)
    elif kind == "StatefulSet":
        desired = int(spec.get("replicas", 1))
        ready = int(status.get("readyReplicas") or 0)
        available = int(status.get("availableReplicas", ready) or 0)
    elif kind == "DaemonSet":
        desired = int(status.get("desiredNumberScheduled") or 0)
        ready = int(status.get("numberReady") or 0)
        available = int(status.get("numberAvailable") or 0)
    else:
        return None

    generation = int(metadata.get("generation") or 0)
    observed_generation = int(status.get("observedGeneration") or 0)

    converged = (
        observed_generation == generation
        and ready == desired
        and available == desired
        and desired >= 0
    )

    return WorkloadRollout(
        workload=OwnedWorkload(
            kind=kind,
            namespace=namespace,
            name=name,
            desired=desired,
            ready=ready,
            available=available,
        ),
        converged=converged,
        generation=generation,
        observed_generation=observed_generation,
    )


def _opt_str(value: Any) -> str | None:
    """str() the value, passing None through."""
    if value is None:
        return None
    return str(value)


def _dict(value: Any) -> dict[str, Any]:
    """Return a string-keyed mapping for a decoded JSON object, else an empty one."""
    if not isinstance(value, dict):
        return {}
    return value
