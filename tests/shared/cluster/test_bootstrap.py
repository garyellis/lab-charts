"""`bootstrap()` and `verify()`: the LocalCluster's ordered releases, through `converge`."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.api.v1alpha1.local_cluster import LocalCluster
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.shared.cluster import bootstrap, session
from chart_manager.shared.cluster.converge import ReleaseFailed
from chart_manager.shared.settings import Settings
from tests.conftest import FakeCommandRunner

from .test_converge import _cmd, _is

CILIUM_MANIFEST = """\
apiVersion: apps/v1
kind: DaemonSet
metadata: {name: cilium, namespace: kube-system}
"""


def _cluster(releases: list[dict[str, object]]) -> LocalCluster:
    return LocalCluster.model_validate(
        {
            "apiVersion": "chartmanager.io/v1alpha1",
            "kind": "LocalCluster",
            "metadata": {"name": "default"},
            "spec": {"cluster": {"config": "kind.yaml"}, "bootstrap": {"releases": releases}},
        }
    )


NETWORK = {
    "type": "local",
    "name": "network",
    "chart": "charts/network",
    "namespace": "kube-system",
    "values": [],
    "timeout": "10m",
    "runtimeValues": {
        "api.host": "${kind.controlPlaneHost}",
        "api.port": "${kind.controlPlanePort}",
    },
    "readiness": {
        "nodesReady": True,
        "workloadsReady": {"namespace": "kube-system", "timeout": "4m"},
    },
}
METRICS = {
    "type": "oci",
    "name": "metrics",
    "chart": "oci://registry.example.test/charts/metrics",
    "version": "1.2.3",
    "namespace": "monitoring",
    "values": [],
    "timeout": "5m",
    "runtimeValues": {"cluster.name": "${kind.clusterName}"},
}


def _runner() -> FakeCommandRunner:
    return (
        FakeCommandRunner()
        .respond(_is("helm", "get", "manifest", "network"), stdout=CILIUM_MANIFEST)
        .respond(_is("docker", "inspect"), stdout="172.18.0.2\n")
    )


def _repo(tmp_path: Path) -> Path:
    chart = tmp_path / "charts/network"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: network\nversion: 0.1.0\n")
    return tmp_path


def _steps(runner: FakeCommandRunner) -> list[tuple[str, ...]]:
    """The installs and waits, in order, without listings and lookups."""
    return [
        cmd
        for cmd in map(_cmd, runner.calls)
        if cmd[:2] == ("helm", "upgrade") or "rollout" in cmd or cmd[1:2] == ("wait",)
    ]


def test_bootstrap_converges_in_order_then_waits_for_nodes_after_the_network(
    tmp_path: Path,
) -> None:
    runner = _runner()
    dev = session.attach("dev", runner=runner, settings=Settings())

    outcomes = bootstrap.bootstrap(dev, _cluster([NETWORK, METRICS]), root=_repo(tmp_path))

    steps = _steps(runner)
    network, rollout, nodes, metrics = steps
    assert network[:4] == ("helm", "upgrade", "--install", "network")
    assert "--set" in network and "api.host=172.18.0.2" in network and "api.port=6443" in network
    assert rollout == (
        "kubectl", "-n", "kube-system", "rollout", "status", "daemonset/cilium", "--timeout=10m",
    )
    assert nodes == ("kubectl", "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=4m")
    assert metrics[:4] == ("helm", "upgrade", "--install", "metrics")
    assert "cluster.name=dev" in metrics
    assert [(o.name, o.namespace) for o in outcomes] == [
        ("network", "kube-system"),
        ("metrics", "monitoring"),
    ]


def test_bootstrap_stops_at_the_first_failed_release(tmp_path: Path) -> None:
    runner = _runner().respond(_is("helm", "upgrade", "--install", "network"), returncode=1)
    dev = session.attach("dev", runner=runner, settings=Settings())

    with pytest.raises(ReleaseFailed, match="network"):
        bootstrap.bootstrap(dev, _cluster([NETWORK, METRICS]), root=_repo(tmp_path))

    assert not any(_cmd(argv)[:4] == ("helm", "upgrade", "--install", "metrics") for argv in runner.calls)


def test_verify_accepts_a_release_in_any_state_and_names_a_missing_one(tmp_path: Path) -> None:
    cluster = _cluster([NETWORK, METRICS])

    bootstrap.verify(
        cluster,
        root=_repo(tmp_path),
        releases={("kube-system", "network"): "failed", ("monitoring", "metrics"): "deployed"},
    )
    with pytest.raises(ChartManagerError) as missing:
        bootstrap.verify(
            cluster, root=tmp_path, releases={("kube-system", "network"): "deployed"}
        )

    assert str(missing.value) == (
        "bootstrap release 'metrics' is not installed in namespace 'monitoring'; "
        "rerun without --skip-requires to converge it"
    )
