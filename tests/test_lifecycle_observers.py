"""Read-only live lifecycle observer tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from chart_manager.services.lifecycle.evidence import ClusterIdentity, TargetCoordinates
from chart_manager.services.lifecycle.models import (
    ActionKind,
    ActionTarget,
    LifecycleAction,
    Workflow,
)
from chart_manager.services.lifecycle.observers import (
    HelmReleaseStatusObserver,
    WorkloadReadinessObserver,
)

NOW = datetime(2026, 7, 26, 12, 30, tzinfo=UTC)
CLUSTER = ClusterIdentity(
    name="chart-manager",
    context="kind-chart-manager",
    uid="cluster-123",
    kubernetes_version="1.31.2",
)


@dataclass(frozen=True)
class _Release:
    name: str
    namespace: str
    revision: int
    status: str


class _Helm:
    def __init__(self, releases: list[_Release]) -> None:
        self.releases = releases
        self.calls = 0

    def list_releases(
        self,
        *,
        all_namespaces: bool = True,
        namespace: str | None = None,
    ) -> list[_Release]:
        self.calls += 1
        assert all_namespaces is True
        assert namespace is None
        return self.releases


class _Kubectl:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[list[str]] = []

    def get_json(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append(args)
        assert timeout is None
        return self.payload


def _action(
    kind: ActionKind,
    *,
    chart: str = "grafana",
    release: str = "grafana",
    namespace: str = "observability",
    digest: str = "sha256:current-input",
) -> LifecycleAction:
    return LifecycleAction(
        action_id=f"cluster-test:{chart}:minimal:{kind}",
        kind=kind,
        target=ActionTarget(
            workflow=Workflow.CLUSTER_TEST,
            chart=chart,
            profile="minimal",
            release=release,
            namespace=namespace,
        ),
        input_digest=digest,
        chart_path=Path("charts") / chart,
    )


def _workload(
    *,
    kind: str = "Deployment",
    name: str = "grafana",
    desired: int = 1,
    ready: int = 1,
    available: int = 1,
    generation: int = 4,
    observed_generation: int = 4,
    release: str = "grafana",
) -> dict[str, Any]:
    if kind == "DaemonSet":
        spec: dict[str, Any] = {}
        status = {
            "desiredNumberScheduled": desired,
            "numberReady": ready,
            "numberAvailable": available,
            "observedGeneration": observed_generation,
        }
    else:
        spec = {"replicas": desired}
        status = {
            "readyReplicas": ready,
            "availableReplicas": available,
            "observedGeneration": observed_generation,
        }
    return {
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "observability",
            "generation": generation,
            "labels": {"app.kubernetes.io/instance": release},
        },
        "spec": spec,
        "status": status,
    }


def test_deployed_release_emits_live_evidence_from_one_snapshot() -> None:
    helm = _Helm(
        [
            _Release("grafana", "observability", 7, "deployed"),
            _Release("loki", "observability", 3, "deployed"),
        ]
    )
    observer = HelmReleaseStatusObserver(
        helm,
        cluster=CLUSTER,
        run_id="live-release-snapshot",
        clock=lambda: NOW,
    )

    grafana = observer.observe(_action(ActionKind.HELM_UPGRADE_INSTALL))
    loki = observer.observe(
        _action(ActionKind.HELM_UPGRADE_INSTALL, chart="loki", release="loki")
    )

    assert helm.calls == 1
    assert grafana is not None
    assert grafana.source == "live"
    assert grafana.verdict == "PASS"
    assert grafana.status == "deployed"
    assert grafana.reason == "ReleaseDeployed"
    assert grafana.detail == "release=grafana namespace=observability revision=7 status=deployed"
    assert grafana.cluster == CLUSTER
    assert loki is not None
    assert "revision=3" in (loki.detail or "")


@pytest.mark.parametrize("release_status", ["failed", "pending-install", "pending-upgrade"])
def test_non_deployed_release_is_observed_as_not_ready(release_status: str) -> None:
    observer = HelmReleaseStatusObserver(
        _Helm([_Release("grafana", "observability", 2, release_status)]),
        cluster=CLUSTER,
        clock=lambda: NOW,
    )

    evidence = observer.observe(_action(ActionKind.HELM_UPGRADE_INSTALL))

    assert evidence is not None
    assert evidence.verdict == "FAIL"
    assert evidence.status == release_status
    assert evidence.reason == "ReleaseNotDeployed"


def test_missing_release_emits_explicit_unknown_evidence() -> None:
    helm = _Helm([_Release("loki", "observability", 1, "deployed")])
    observer = HelmReleaseStatusObserver(helm, cluster=CLUSTER)

    evidence = observer.observe(_action(ActionKind.HELM_UPGRADE_INSTALL))

    assert evidence is not None
    assert evidence.verdict == "UNKNOWN"
    assert evidence.status == "missing"
    assert evidence.reason == "ReleaseNotFound"
    assert helm.calls == 1


def test_unrelated_static_action_does_not_query_cluster() -> None:
    helm = _Helm([_Release("grafana", "observability", 1, "deployed")])
    kubectl = _Kubectl({"items": [_workload()]})
    action = _action(ActionKind.RENDER)

    assert HelmReleaseStatusObserver(helm, cluster=CLUSTER).observe(action) is None
    assert WorkloadReadinessObserver(kubectl, cluster=CLUSTER).observe(action) is None
    assert helm.calls == 0
    assert kubectl.calls == []


def test_ready_workloads_emit_pass_from_one_read_only_query() -> None:
    kubectl = _Kubectl(
        {
            "items": [
                _workload(),
                _workload(
                    kind="DaemonSet",
                    name="alloy",
                    desired=3,
                    ready=3,
                    available=3,
                ),
            ]
        }
    )
    observer = WorkloadReadinessObserver(
        kubectl,
        cluster=CLUSTER,
        run_id="live-workloads",
        clock=lambda: NOW,
    )

    evidence = observer.observe(_action(ActionKind.WORKLOAD_READY))

    assert kubectl.calls == [
        [
            "-n",
            "observability",
            "get",
            "deployment,statefulset,daemonset",
            "-o",
            "json",
        ]
    ]
    assert evidence is not None
    assert evidence.verdict == "PASS"
    assert evidence.status == "ready"
    assert evidence.reason == "WorkloadsReady"
    assert "Deployment/grafana ready=1/1" in (evidence.detail or "")
    assert "DaemonSet/alloy ready=3/3" in (evidence.detail or "")


def test_pending_workload_emits_failed_current_fact() -> None:
    kubectl = _Kubectl(
        {"items": [_workload(desired=2, ready=1, available=1, observed_generation=3)]}
    )
    observer = WorkloadReadinessObserver(kubectl, cluster=CLUSTER, clock=lambda: NOW)

    evidence = observer.observe(_action(ActionKind.WORKLOAD_READY))

    assert evidence is not None
    assert evidence.verdict == "FAIL"
    assert evidence.status == "not-ready"
    assert evidence.reason == "WorkloadsNotReady"
    assert "ready=1/2" in (evidence.detail or "")
    assert "generation=3/4" in (evidence.detail or "")


def test_empty_supported_workload_list_is_unknown_not_pass() -> None:
    kubectl = _Kubectl({"items": []})
    observer = WorkloadReadinessObserver(kubectl, cluster=CLUSTER, clock=lambda: NOW)

    evidence = observer.observe(_action(ActionKind.WORKLOAD_READY))

    assert evidence is not None
    assert evidence.verdict == "UNKNOWN"
    assert evidence.status == "missing"
    assert evidence.reason == "OwnedWorkloadsNotFound"
    assert len(kubectl.calls) == 1


def test_unrelated_ready_workloads_do_not_produce_a_pass() -> None:
    kubectl = _Kubectl({"items": [_workload(release="loki")]})
    observer = WorkloadReadinessObserver(kubectl, cluster=CLUSTER, clock=lambda: NOW)

    evidence = observer.observe(_action(ActionKind.WORKLOAD_READY))

    assert evidence is not None
    assert evidence.verdict == "UNKNOWN"
    assert evidence.reason == "OwnedWorkloadsNotFound"


def test_observer_preserves_action_target_and_input_digest() -> None:
    action = _action(
        ActionKind.HELM_UPGRADE_INSTALL,
        digest="sha256:exact-plan-input",
    )
    observer = HelmReleaseStatusObserver(
        _Helm([_Release("grafana", "observability", 9, "deployed")]),
        cluster=CLUSTER,
        clock=lambda: NOW,
    )

    evidence = observer.observe(action)

    assert evidence is not None
    assert evidence.action_id == action.action_id
    assert evidence.action_kind == "helm-upgrade-install"
    assert evidence.input_digest == "sha256:exact-plan-input"
    assert evidence.target == TargetCoordinates(
        workflow="cluster-test",
        chart="grafana",
        profile="minimal",
        release="grafana",
        namespace="observability",
    )
    assert evidence.started_at == NOW
    assert evidence.finished_at == NOW
    assert evidence.recorded_at == NOW
