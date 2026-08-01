"""Generic, ordered LocalCluster bootstrap execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chart_manager.api.local.v1alpha1 import LocalCluster
from chart_manager.integrations.helm import UpgradeResult
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.clusters.bootstrap import LocalBootstrapExecutor
from chart_manager.services.clusters.environment import EnvironmentHandle
from chart_manager.services.lifecycle.plan_projection import ExternallySatisfiedLifecycle


def _cluster(releases: list[dict[str, object]]) -> LocalCluster:
    return LocalCluster.model_validate(
        {
            "apiVersion": "local.chartmanager.io/v1alpha1",
            "kind": "LocalCluster",
            "metadata": {"name": "default"},
            "spec": {
                "cluster": {"config": "kind-config.yaml"},
                "bootstrap": {"releases": releases},
            },
        }
    )


class _Kind:
    def __init__(self) -> None:
        self.ip_calls: list[str] = []

    def control_plane_ip(self, name: str) -> str:
        self.ip_calls.append(name)
        return "172.18.0.2"


class _Kubectl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def wait_nodes_ready(self, *, timeout: str) -> None:
        self.calls.append(("nodes", timeout))

    def wait_workloads_ready(self, namespace: str, *, timeout: str) -> None:
        self.calls.append((namespace, timeout))


class _Helm:
    def __init__(
        self,
        *,
        fail_on: str | None = None,
        fail_lint: bool = False,
    ) -> None:
        self.fail_on = fail_on
        self.fail_lint = fail_lint
        self.calls: list[tuple[str, object, dict[str, Any]]] = []
        self.dependencies: list[Path] = []
        self.lints: list[tuple[Path, list[Path]]] = []

    def dependency_update_if_stale(self, chart: Path) -> bool:
        self.dependencies.append(chart)
        return False

    def lint(self, chart: Path, values: list[Path] | None = None) -> None:
        self.lints.append((chart, values or []))
        if self.fail_lint:
            raise ExternalCommandError("lint failed")

    def upgrade_install(
        self,
        name: str,
        chart: object,
        **kwargs: Any,
    ) -> UpgradeResult:
        self.calls.append((name, chart, kwargs))
        if name == self.fail_on:
            raise ExternalCommandError(f"{name} failed")
        return UpgradeResult("applied", None, 1, "")


def _executor(
    tmp_path: Path,
    *,
    helm: _Helm,
    kind: _Kind | None = None,
    kubectl: _Kubectl | None = None,
) -> tuple[LocalBootstrapExecutor, _Kind, _Kubectl]:
    kind = kind or _Kind()
    kubectl = kubectl or _Kubectl()
    return (
        LocalBootstrapExecutor(
            tmp_path,
            helm=helm,  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            kubectl=kubectl,  # type: ignore[arg-type]
        ),
        kind,
        kubectl,
    )


def test_ordered_bootstrap_injects_only_declared_kind_facts_and_waits(
    tmp_path: Path,
) -> None:
    local_chart = tmp_path / "charts/network"
    local_chart.mkdir(parents=True)
    values = tmp_path / "values/network.yaml"
    values.parent.mkdir()
    values.write_text("{}\n", encoding="utf-8")
    cluster = _cluster(
        [
            {
                "type": "local",
                "name": "network",
                "chart": "charts/network",
                "namespace": "kube-system",
                "values": ["values/network.yaml"],
                "timeout": "10m",
                "runtimeValues": {
                    "api.host": "${kind.controlPlaneHost}",
                    "api.port": "${kind.controlPlanePort}",
                },
                "readiness": {
                    "nodesReady": True,
                    "workloadsReady": {"namespace": "kube-system", "timeout": "4m"},
                },
            },
            {
                "type": "oci",
                "name": "metrics",
                "chart": "oci://registry.example.test/charts/metrics",
                "version": "1.2.3",
                "namespace": "monitoring",
                "values": [],
                "timeout": "5m",
                "runtimeValues": {"cluster.name": "${kind.clusterName}"},
            },
        ]
    )
    helm = _Helm()
    executor, kind, kubectl = _executor(tmp_path, helm=helm)

    outcomes = executor.execute(
        cluster,
        environment=EnvironmentHandle(
            identity="dev-cluster",
            context="kind-dev-cluster",
            provider_type="kind",
        ),
    )

    assert [call[0] for call in helm.calls] == ["network", "metrics"]
    assert helm.calls[0][2]["sets"] == {
        "api.host": "172.18.0.2",
        "api.port": "6443",
    }
    assert helm.calls[1][2]["sets"] == {"cluster.name": "dev-cluster"}
    assert helm.calls[1][2]["version"] == "1.2.3"
    assert kind.ip_calls == ["dev-cluster"]
    assert kubectl.calls == [("nodes", "4m"), ("kube-system", "4m")]
    assert [outcome.name for outcome in outcomes] == ["network", "metrics"]


def test_bootstrap_stops_at_the_first_failed_release(tmp_path: Path) -> None:
    chart = tmp_path / "charts/network"
    chart.mkdir(parents=True)
    cluster = _cluster(
        [
            {
                "type": "local",
                "name": "network",
                "chart": "charts/network",
                "namespace": "kube-system",
                "values": [],
                "timeout": "5m",
            },
            {
                "type": "oci",
                "name": "never-run",
                "chart": "oci://registry.example.test/charts/never-run",
                "version": "1.0.0",
                "namespace": "default",
                "values": [],
                "timeout": "5m",
            },
        ]
    )
    helm = _Helm(fail_on="network")
    executor, _, kubectl = _executor(tmp_path, helm=helm)

    with pytest.raises(ExternalCommandError, match="network failed"):
        executor.execute(
            cluster,
            environment=EnvironmentHandle(
                identity="dev",
                context="kind-dev",
                provider_type="kind",
            ),
        )

    assert [call[0] for call in helm.calls] == ["network"]
    assert kubectl.calls == []


def test_raw_local_and_oci_releases_never_claim_managed_lifecycle_identity(
    tmp_path: Path,
) -> None:
    (tmp_path / "charts/network").mkdir(parents=True)
    cluster = _cluster(
        [
            {
                "type": "local",
                "name": "network",
                "chart": "charts/network",
                "namespace": "kube-system",
                "values": [],
                "timeout": "5m",
            },
            {
                "type": "oci",
                "name": "network",
                "chart": "oci://registry.example.test/charts/network",
                "version": "1.2.3",
                "namespace": "monitoring",
                "values": [],
                "timeout": "5m",
            },
        ]
    )
    executor, _, _ = _executor(tmp_path, helm=_Helm())

    assert executor.preflight(cluster) == frozenset()


def test_preflight_resolves_bootstrap_lifecycle_identities(tmp_path: Path) -> None:
    chart = tmp_path / "charts/network"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: network\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (chart / "chart-lifecycle.yaml").write_text(
        """
apiVersion: lifecycle.chartmanager.io/v1alpha1
kind: ChartLifecycle
metadata: {name: network}
spec:
  clusterTest:
    profiles:
      minimal:
        namespace: kube-system
        values: []
""".lstrip(),
        encoding="utf-8",
    )
    cluster = _cluster(
        [
            {
                "type": "lifecycle",
                "chart": "charts/network",
                "profile": "minimal",
            }
        ]
    )
    executor, _, _ = _executor(tmp_path, helm=_Helm())

    identities = executor.preflight(cluster)

    assert identities == frozenset(
        {
            ExternallySatisfiedLifecycle(
                chart_path=chart.resolve(),
                chart="network",
                profile="minimal",
                namespace="kube-system",
            )
        }
    )


def test_bootstrap_lint_failure_prevents_any_install(tmp_path: Path) -> None:
    chart = tmp_path / "charts/network"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: network\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (chart / "chart-lifecycle.yaml").write_text(
        """
apiVersion: lifecycle.chartmanager.io/v1alpha1
kind: ChartLifecycle
metadata: {name: network}
spec:
  clusterTest:
    profiles:
      minimal: {namespace: kube-system, values: []}
""".lstrip(),
        encoding="utf-8",
    )
    cluster = _cluster(
        [{"type": "lifecycle", "chart": "charts/network", "profile": "minimal"}]
    )
    helm = _Helm(fail_lint=True)
    executor, _, _ = _executor(tmp_path, helm=helm)

    with pytest.raises(ExternalCommandError, match="lint failed"):
        executor.preflight(cluster, lint=True)

    assert len(helm.lints) == 1
    assert helm.calls == []
    assert helm.dependencies == [chart]


def test_preflight_resolves_every_release_before_linting_any(
    tmp_path: Path,
) -> None:
    chart = tmp_path / "charts/network"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: network\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (chart / "chart-lifecycle.yaml").write_text(
        """
apiVersion: lifecycle.chartmanager.io/v1alpha1
kind: ChartLifecycle
metadata: {name: network}
spec:
  clusterTest:
    profiles:
      minimal: {namespace: kube-system, values: []}
""".lstrip(),
        encoding="utf-8",
    )
    cluster = _cluster(
        [
            {"type": "lifecycle", "chart": "charts/network", "profile": "minimal"},
            {"type": "lifecycle", "chart": "charts/network", "profile": "missing"},
        ]
    )
    helm = _Helm()
    executor, _, _ = _executor(tmp_path, helm=helm)

    with pytest.raises(ChartManagerError, match="unknown profile 'missing'"):
        executor.preflight(cluster, lint=True)

    assert helm.lints == []
    assert helm.calls == []
    assert helm.dependencies == []
