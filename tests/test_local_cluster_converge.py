"""`local up` convergence: what the clients address, and what a failure stops.

Two properties that only the mutating path can show:
  * every Helm call made *after* the environment is resolved goes through the
    clients bound to that environment's kubecontext -- bootstrap included,
    which is the phase that used to run through the pre-rebind clients;
  * the continue-on-error contract `_install_plan` documents holds for every
    cluster call it makes, not only for the ones that happen to sit inside
    the try block.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chart_manager.api.lifecycle.v1alpha1 import ClusterTestProfile, ClusterTestSpec
from chart_manager.domain.charts import (
    ChartMetadata,
    ClusterTestChart,
    HelmChart,
)
from chart_manager.domain.install_plan import InstallPlanEntry
from chart_manager.domain.local_resources import ResolvedChartTarget
from chart_manager.integrations.helm import ReleaseInfo, UpgradeResult
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.services.clusters.development import (
    DevelopmentClusterService,
    RunSummary,
)
from chart_manager.services.clusters.environment import BoundClients


class _Helm:
    """A Helm bound to one kubecontext; every install records which one."""

    def __init__(self, context: str, calls: list[tuple[str, str]]) -> None:
        self.context = context
        self.calls = calls

    def list_releases(
        self, *, all_namespaces: bool = True, namespace: str | None = None
    ) -> list[ReleaseInfo]:
        return []

    def dependency_update_if_stale(self, _path: Path) -> bool:
        return False

    def upgrade_install(
        self, release: str, _chart: Any, **_kwargs: Any
    ) -> UpgradeResult:
        self.calls.append((self.context, release))
        return UpgradeResult(status="applied", revision_before=0, revision_after=1, output="")


class _Kubectl:
    def __init__(self, *, namespace_raises: dict[str, Exception] | None = None) -> None:
        self._namespace_raises = namespace_raises or {}
        self.namespaces: list[str] = []

    def wait_apiserver_ready(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def wait_workloads_ready(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def wait_deployment_available(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def create_namespace(self, namespace: str) -> None:
        self.namespaces.append(namespace)
        error = self._namespace_raises.get(namespace)
        if error is not None:
            raise error

    def diagnostics(self, _namespace: str) -> str:
        return ""

    def list_virtualservice_hosts(self) -> list[str]:
        return []


class _Kind:
    def __init__(self) -> None:
        self.ensure_calls: list[str] = []

    def ensure_cluster(self, name: str, *, config: Path | None = None) -> None:
        self.ensure_calls.append(name)

    def container_host_ports(self, _name: str) -> set[int]:
        return set()


class _Expose:
    def stop(self, _cluster: str) -> int | None:
        return None


def _stub_chart(name: str, *, namespace: str) -> ClusterTestChart:
    return ClusterTestChart(
        chart=HelmChart(
            name=name,
            path=Path(f"/tmp/{name}"),
            metadata=ChartMetadata(name, "0.0.0", "application", ()),
        ),
        spec=ClusterTestSpec(
            profiles={
                "minimal": ClusterTestProfile(
                    namespace=namespace,
                    values=[],
                    timeout="1m",
                    requires=[],
                    helmTest=False,
                )
            },
            dependentTests=[],
        ),
    )


def _repository(tmp_path: Path) -> None:
    """A LocalCluster with one raw bootstrap release plus a lifecycle target."""
    (tmp_path / "kind-config.yaml").write_text("kind: Cluster\n", encoding="utf-8")
    cni = tmp_path / "charts" / "cni"
    cni.mkdir(parents=True)
    (cni / "Chart.yaml").write_text(
        "apiVersion: v2\nname: cni\nversion: 1.0.0\n", encoding="utf-8"
    )
    grafana = tmp_path / "charts" / "grafana"
    grafana.mkdir(parents=True)
    (grafana / "Chart.yaml").write_text(
        "apiVersion: v2\nname: grafana\nversion: 1.0.0\n", encoding="utf-8"
    )
    (grafana / "chart-lifecycle.yaml").write_text(
        (
            "apiVersion: lifecycle.chartmanager.io/v1alpha1\n"
            "kind: ChartLifecycle\n"
            "metadata: {name: grafana}\n"
            "spec:\n"
            "  clusterTest:\n"
            "    profiles:\n"
            "      minimal:\n"
            "        namespace: observability\n"
            "        values: []\n"
        ),
        encoding="utf-8",
    )
    config = tmp_path / ".chart-manager" / "local-cluster.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """
apiVersion: local.chartmanager.io/v1alpha1
kind: LocalCluster
metadata: {name: default}
spec:
  cluster: {config: kind-config.yaml}
  bootstrap:
    releases:
      - type: local
        name: cni
        chart: charts/cni
        namespace: kube-system
        values: []
        timeout: 1m
""".lstrip(),
        encoding="utf-8",
    )


def test_bootstrap_installs_through_the_context_bound_clients(tmp_path: Path) -> None:
    """Bootstrap must address the cluster `up` just resolved, not the ambient one.

    The executor used to be constructed before `_ensure_environment` rebound
    the clients, so every bootstrap `helm upgrade --install` (cilium,
    cert-manager, istio) went to whatever `current-context` the workstation
    happened to hold. It looked correct only because `kind create cluster`
    mutates `current-context` as a side effect -- a `down` -> `up` against an
    existing cluster, or a workstation pointed elsewhere, converged the wrong
    cluster.
    """
    _repository(tmp_path)
    calls: list[tuple[str, str]] = []
    kubectl = _Kubectl()

    def clients(handle: Any) -> BoundClients:
        return BoundClients(
            helm=_Helm(handle.context, calls),  # type: ignore[arg-type]
            kubectl=kubectl,  # type: ignore[arg-type]
            expose=_Expose(),  # type: ignore[arg-type]
        )

    service = DevelopmentClusterService(
        tmp_path,
        helm=_Helm("ambient", calls),  # type: ignore[arg-type]
        kind=_Kind(),  # type: ignore[arg-type]
        kubectl=kubectl,  # type: ignore[arg-type]
        expose=_Expose(),  # type: ignore[arg-type]
        client_factory=clients,
    )

    result = service.up_target(
        ResolvedChartTarget(name="grafana", path=(tmp_path / "charts" / "grafana").resolve()),
        profile="minimal",
        cluster_name="lab",
    )

    assert result.ok
    # Bootstrap first, then the target -- both on the resolved context, and
    # nothing at all on the ambient one.
    assert calls == [("kind-lab", "cni"), ("kind-lab", "grafana")]


def test_a_failed_namespace_create_fails_one_chart_and_the_converge_continues(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """`kubectl create namespace` is a cluster call like any other in the loop.

    It tolerates "already exists" via `check=False`, but a CommandTimeout or a
    missing binary still raises, and it used to sit above the try -- so one
    unreachable apiserver aborted an 18-chart converge instead of recording a
    single failed row.
    """
    calls: list[tuple[str, str]] = []
    kubectl = _Kubectl(
        namespace_raises={"observability": ExternalCommandError("timed out")}
    )
    service = DevelopmentClusterService(
        tmp_path,
        helm=_Helm("kind-lab", calls),  # type: ignore[arg-type]
        kind=_Kind(),  # type: ignore[arg-type]
        kubectl=kubectl,  # type: ignore[arg-type]
        expose=_Expose(),  # type: ignore[arg-type]
    )
    charts = {
        "loki": _stub_chart("loki", namespace="observability"),
        "grafana": _stub_chart("grafana", namespace="monitoring"),
    }

    class _Catalog:
        def get(self, name: str) -> Any:
            return charts[name]

        def value_paths(self, _chart: Any, _profile: str) -> list[Path]:
            return []

    summary = RunSummary()
    service._install_plan(
        [
            InstallPlanEntry(chart="loki", profile="minimal"),
            InstallPlanEntry(chart="grafana", profile="minimal"),
        ],
        default_namespace="default",
        installed_keys=set(),
        namespaces_created=set(),
        summary=summary,
        skip_installed=False,
        cluster_tests=_Catalog(),  # type: ignore[arg-type]
    )

    assert [(f.chart, f.namespace) for f in summary.failed] == [("loki", "observability")]
    assert "timed out" in summary.failed[0].error
    # The next chart still converged, on its own namespace.
    assert [(o.chart, o.namespace) for o in summary.applied] == [("grafana", "monitoring")]
    assert calls == [("kind-lab", "grafana")]


def test_a_continue_on_error_failure_names_the_chart_in_the_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The failed row is a `RunSummary` entry; the log is the parallel channel.

    Continue-on-error means the process exits 0 for a converge that only half
    happened, and the narration that reported it is presentation -- optional,
    unlevelled, and gone under `-o json`. An ERROR naming the chart is what
    makes the partial converge findable afterwards.
    """
    kubectl = _Kubectl(
        namespace_raises={"observability": ExternalCommandError("timed out")}
    )
    service = DevelopmentClusterService(
        tmp_path,
        helm=_Helm("kind-lab", []),  # type: ignore[arg-type]
        kind=_Kind(),  # type: ignore[arg-type]
        kubectl=kubectl,  # type: ignore[arg-type]
        expose=_Expose(),  # type: ignore[arg-type]
    )
    charts = {"loki": _stub_chart("loki", namespace="observability")}

    class _Catalog:
        def get(self, name: str) -> Any:
            return charts[name]

        def value_paths(self, _chart: Any, _profile: str) -> list[Path]:
            return []

    summary = RunSummary()
    with caplog.at_level("ERROR"):
        service._install_plan(
            [InstallPlanEntry(chart="loki", profile="minimal")],
            default_namespace="default",
            installed_keys=set(),
            namespaces_created=set(),
            summary=summary,
            skip_installed=False,
            cluster_tests=_Catalog(),  # type: ignore[arg-type]
        )

    assert [f.chart for f in summary.failed] == ["loki"]
    [record] = [r for r in caplog.records if r.levelname == "ERROR"]
    rendered = record.getMessage()
    assert "chart apply failed" in rendered
    assert "chart=loki" in rendered
    assert "namespace=observability" in rendered
    # The exception detail, not just the fact of a failure.
    assert "timed out" in rendered
