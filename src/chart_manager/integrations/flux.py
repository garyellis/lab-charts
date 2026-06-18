"""Thin kubectl-backed client for reading Flux HelmRelease state.

All methods are read-only, stateless, and safe under Python-thread
concurrency; the caller owns kubeconfig/context and any external
rate-limiting (recommended bound ~8 concurrent calls per kubeconfig due
to exec-auth-plugin token-cache races on EKS/GKE). No retries, no waits
-- the service layer owns budgets.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError

_LOG = logging.getLogger(__name__)
_FLUX_GROUP_PREFIX = "helm.toolkit.fluxcd.io/"


@dataclass(frozen=True)
class HelmReleaseRef:
    name: str
    namespace: str
    api_version: str
    release_name: str
    storage_namespace: str
    target_namespace: str


@dataclass(frozen=True)
class ConditionSnapshot:
    type: str
    status: str
    reason: str
    message: str
    last_transition_time: datetime | None


@dataclass(frozen=True)
class HelmReleaseStatus:
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
        for cond in self.conditions:
            if cond.type == type_:
                return cond
        return None

    @property
    def ready(self) -> ConditionSnapshot | None:
        return self.condition("Ready")

    @property
    def released(self) -> ConditionSnapshot | None:
        return self.condition("Released")

    @property
    def test_success(self) -> ConditionSnapshot | None:
        return self.condition("TestSuccess")


@dataclass(frozen=True)
class OwnedWorkload:
    kind: str
    namespace: str
    name: str
    desired: int
    ready: int
    available: int


@dataclass(frozen=True)
class WorkloadRollout:
    workload: OwnedWorkload
    converged: bool
    generation: int
    observed_generation: int


class Flux:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        context: str | None = None,
    ) -> None:
        self.runner = runner or CommandRunner()
        self._context = context

    def list(
        self,
        *,
        namespace: str | None = None,
        timeout: float | None = None,
    ) -> list[HelmReleaseRef]:
        args = ["kubectl", "get", "helmreleases.helm.toolkit.fluxcd.io"]
        if namespace is None:
            args.append("-A")
        else:
            args.extend(["-n", namespace])
        args.extend(["-o", "json"])
        payload = self._get_json(self._with_context(args), timeout=timeout)
        refs: list[HelmReleaseRef] = []
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
        args = [
            "kubectl", "-n", ref.namespace, "get",
            "helmreleases.helm.toolkit.fluxcd.io", ref.name, "-o", "json",
        ]
        result = self.runner.run(self._with_context(args), timeout=timeout)
        observed_at = datetime.now(UTC)
        payload = _parse_json(result.stdout)
        return _status_from_item(payload, ref, observed_at)

    def list_owned_workloads(
        self,
        ref: HelmReleaseRef,
        *,
        timeout: float | None = None,
    ) -> list[WorkloadRollout]:
        selector = (
            f"helm.toolkit.fluxcd.io/name={ref.name},"
            f"helm.toolkit.fluxcd.io/namespace={ref.namespace}"
        )
        args = [
            "kubectl", "get", "deployment,statefulset,daemonset",
            "-A", "-l", selector, "-o", "json",
        ]
        payload = self._get_json(self._with_context(args), timeout=timeout)
        rollouts: list[WorkloadRollout] = []
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
    ) -> list[tuple[str, str, str]]:
        base = (
            f"helm.toolkit.fluxcd.io/name={ref.name},"
            f"helm.toolkit.fluxcd.io/namespace={ref.namespace}"
        )
        seen: set[tuple[str, str]] = set()
        pods: list[tuple[str, str, str]] = []
        for hook in ("test", "test-success"):
            args = [
                "kubectl", "-n", ref.target_namespace, "get", "pods",
                "-l", f"{base},helm.sh/hook={hook}", "-o", "json",
            ]
            payload = self._get_json(self._with_context(args), timeout=timeout)
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

    def delete_pod(
        self,
        namespace: str,
        name: str,
        *,
        timeout: float | None = None,
    ) -> None:
        self.runner.run(
            self._with_context([
                "kubectl", "-n", namespace, "delete", "pod", name,
                "--ignore-not-found",
            ]),
            timeout=timeout,
        )

    def namespace_events(
        self,
        namespace: str,
        *,
        timeout: float | None = None,
    ) -> str:
        result = self.runner.run(
            self._with_context([
                "kubectl", "get", "events", "-n", namespace,
                "--sort-by=.lastTimestamp",
            ]),
            check=False,
            timeout=timeout,
        )
        return result.stdout + result.stderr

    def workload_events(
        self,
        kind: str,
        namespace: str,
        name: str,
        *,
        timeout: float | None = None,
    ) -> str:
        result = self.runner.run(
            self._with_context([
                "kubectl", "get", "events", "-n", namespace,
                "--field-selector",
                f"involvedObject.name={name},involvedObject.kind={kind}",
                "--sort-by=.lastTimestamp",
            ]),
            check=False,
            timeout=timeout,
        )
        return result.stdout + result.stderr

    def pod_logs(
        self,
        namespace: str,
        name: str,
        *,
        container: str | None = None,
        tail: int = 200,
        previous: bool = False,
        timeout: float | None = None,
    ) -> str:
        args = [
            "kubectl", "-n", namespace, "logs", name,
            f"--tail={tail}",
        ]
        if container is not None:
            args.extend(["-c", container])
        if previous:
            args.append("--previous")
        result = self.runner.run(self._with_context(args), check=False, timeout=timeout)
        if result.returncode == 0:
            return result.stdout
        stderr = result.stderr or ""
        if "NotFound" in stderr or "not found" in stderr:
            _LOG.warning(
                "pod logs unavailable",
                extra={
                    "namespace": namespace,
                    "pod": name,
                    "reason": stderr.strip()[:200],
                },
            )
            return ""
        raise ExternalCommandError(
            f"command failed ({result.returncode}): {' '.join(args)}\n{stderr.strip()}",
            stderr=stderr,
            returncode=result.returncode,
        )

    def _get_json(
        self,
        args: list[str],
        *,
        timeout: float | None,
    ) -> dict[str, Any]:
        result = self.runner.run(args, timeout=timeout)
        return _parse_json(result.stdout)

    def _with_context(self, args: list[str]) -> list[str]:
        if self._context is None:
            return args
        return [*args, "--context", self._context]


def _parse_json(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        snippet = (stdout or "")[:200]
        raise ChartManagerError(
            f"failed to parse kubectl JSON output: {exc}; payload[:200]={snippet!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise ChartManagerError(
            f"kubectl JSON payload was not an object: {stdout[:200]!r}"
        )
    return payload


def _ref_from_item(item: Any) -> HelmReleaseRef | None:
    if not isinstance(item, dict):
        return None
    api_version = item.get("apiVersion", "")
    # Match by group prefix so v2beta1/v2beta2/v2 all flow through one path.
    if not (isinstance(api_version, str) and api_version.startswith(_FLUX_GROUP_PREFIX)):
        return None
    metadata = item.get("metadata") or {}
    name = str(metadata.get("name") or "")
    namespace = str(metadata.get("namespace") or "")
    if not name or not namespace:
        return None
    spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
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
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}

    chart_spec: dict[str, Any] = {}
    spec_chart = spec.get("chart") if isinstance(spec.get("chart"), dict) else {}
    if isinstance(spec_chart.get("spec"), dict):
        chart_spec = spec_chart["spec"]

    history = status.get("history") if isinstance(status.get("history"), list) else []
    history_chart_version: str | None = None
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
    return ConditionSnapshot(
        type=str(item.get("type") or ""),
        status=str(item.get("status") or ""),
        reason=str(item.get("reason") or ""),
        message=str(item.get("message") or ""),
        last_transition_time=_parse_iso8601(item.get("lastTransitionTime")),
    )


def _parse_iso8601(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _rollout_from_item(item: Any) -> WorkloadRollout | None:
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "")
    metadata = item.get("metadata") or {}
    namespace = str(metadata.get("namespace") or "")
    name = str(metadata.get("name") or "")
    if not name or not namespace:
        return None
    spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
    status = item.get("status") if isinstance(item.get("status"), dict) else {}

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
    if value is None:
        return None
    return str(value)
