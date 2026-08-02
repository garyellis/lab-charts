"""`local status` snapshots and the `--dry-run` plan, at the service boundary.

Two read-only capabilities, tested for the same property: they answer
without mutating, and they answer *completely* even when the cluster cannot
be reached. A status that raised on a stopped cluster would be useless
exactly when it is most wanted, and a plan that needed a running cluster to
resolve would not be a plan.

The fakes mirror `tests/test_lab_access_hints.py`'s -- same shape, plus the
`clusters()` / `status()` reads only these two paths make.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chart_manager.integrations.helm import ReleaseInfo
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.clusters.development import (
    DevelopmentClusterService,
    status_to_dict,
)
from chart_manager.services.clusters.environment import BoundClients
from chart_manager.services.expose import ExposeStatus
from chart_manager.services.local_resources import ResolvedChartTarget


class _Helm:
    """Only `list_releases` is reached; a raise models an unreachable cluster."""

    def __init__(
        self,
        *,
        releases: list[ReleaseInfo] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._releases = releases or []
        self._raises = raises
        self.calls = 0

    def list_releases(
        self, *, all_namespaces: bool = True, namespace: str | None = None
    ) -> list[ReleaseInfo]:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return list(self._releases)

    def dependency_update_if_stale(self, _path: Path) -> bool:
        return False

    def upgrade_install(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a read-only path installed something")


class _Kubectl:
    def __init__(
        self,
        *,
        hosts: list[str] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._hosts = hosts or []
        self._raises = raises

    def list_virtualservice_hosts(self) -> list[str]:
        if self._raises is not None:
            raise self._raises
        return list(self._hosts)


class _Kind:
    def __init__(self, *, host_ports: set[int] | None = None) -> None:
        self._host_ports = host_ports or set()
        self.ensure_calls: list[str] = []

    def clusters(self) -> list[str]:
        return ["chart-manager"]

    def ensure_cluster(self, name: str, *, config: Path | None = None) -> None:
        self.ensure_calls.append(name)

    def container_host_ports(self, _name: str) -> set[int]:
        return set(self._host_ports)

    def stop_cluster(self, _name: str) -> bool:
        raise AssertionError("a read-only path stopped the cluster")

    def delete_cluster(self, _name: str) -> bool:
        raise AssertionError("a read-only path deleted the cluster")


class _AbsentKind(_Kind):
    def clusters(self) -> list[str]:
        return []


class _Expose:
    def __init__(self, *, pid: int | None = None) -> None:
        self._pid = pid

    def status(self, cluster_name: str) -> ExposeStatus | None:
        if self._pid is None:
            return None
        return ExposeStatus(
            cluster_name=cluster_name,
            pid=self._pid,
            service="istio-ingress/gateway",
            ports=["443:443"],
            log=Path("/tmp/pf.log"),
        )

    def stop(self, _cluster: str) -> int | None:
        raise AssertionError("a read-only path stopped the port-forward")


def _local_cluster(root: Path, *, host_ports: list[int] | None = None) -> None:
    """Write the LocalCluster plus the kind config its `spec.cluster` names."""
    mappings = "".join(
        f"      - containerPort: {port}\n        hostPort: {port}\n"
        for port in host_ports or []
    )
    (root / "kind-config.yaml").write_text(
        "kind: Cluster\nnodes:\n  - role: control-plane\n"
        + ("    extraPortMappings:\n" + mappings if mappings else ""),
        encoding="utf-8",
    )
    config = root / ".chart-manager" / "local-cluster.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """
apiVersion: local.chartmanager.io/v1alpha1
kind: LocalCluster
metadata: {name: default}
spec:
  cluster: {config: kind-config.yaml}
  bootstrap: {releases: []}
""".lstrip(),
        encoding="utf-8",
    )


def _chart(root: Path, name: str, *, namespace: str = "observability") -> Path:
    path = root / "charts" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "Chart.yaml").write_text(
        f"apiVersion: v2\nname: {name}\nversion: 1.0.0\n", encoding="utf-8"
    )
    (path / "chart-lifecycle.yaml").write_text(
        (
            "apiVersion: lifecycle.chartmanager.io/v1alpha1\n"
            "kind: ChartLifecycle\n"
            f"metadata: {{name: {name}}}\n"
            "spec:\n"
            "  clusterTest:\n"
            "    profiles:\n"
            "      minimal:\n"
            f"        namespace: {namespace}\n"
            "        values: []\n"
        ),
        encoding="utf-8",
    )
    return path


def _service(
    root: Path,
    *,
    helm: _Helm | None = None,
    kind: _Kind | None = None,
    kubectl: _Kubectl | None = None,
    expose: _Expose | None = None,
    client_factory: Any | None = None,
) -> DevelopmentClusterService:
    return DevelopmentClusterService(
        root,
        helm=helm or _Helm(),  # type: ignore[arg-type]
        kind=kind or _Kind(),  # type: ignore[arg-type]
        kubectl=kubectl or _Kubectl(),  # type: ignore[arg-type]
        expose=expose or _Expose(),  # type: ignore[arg-type]
        client_factory=client_factory,
    )


# ----- status ---------------------------------------------------------------


def test_status_reports_an_absent_cluster_without_asking_anything_else(
    tmp_path: Path,
) -> None:
    """`exists: false` is an answer, not a failure -- and it stops the probing.

    Querying Helm against a cluster that is not there produces a kubeconfig
    error that says nothing `exists: false` did not, only louder.
    """
    _local_cluster(tmp_path)
    helm = _Helm()

    status = _service(tmp_path, helm=helm, kind=_AbsentKind()).status("chart-manager")

    assert status.exists is False
    assert status.cluster_name == "chart-manager"
    assert status.releases == ()
    assert status.releases_error is None
    assert helm.calls == 0


def test_status_reports_releases_urls_and_the_port_forward(tmp_path: Path) -> None:
    _local_cluster(tmp_path)
    helm = _Helm(
        releases=[
            ReleaseInfo(name="loki", namespace="observability", revision=3, status="deployed"),
            ReleaseInfo(name="cert-manager", namespace="cert-manager", revision=1, status="failed"),
        ]
    )
    kubectl = _Kubectl(hosts=["loki.localhost", "grafana.localhost"])

    status = _service(
        tmp_path, helm=helm, kubectl=kubectl, expose=_Expose(pid=4242)
    ).status("chart-manager")

    assert status.exists is True
    assert status.context == "kind-chart-manager"
    assert status.provider == "kind"
    # Sorted by (namespace, name): a document a caller diffs between runs
    # must not reorder with helm's storage driver.
    assert [(r.namespace, r.name, r.status) for r in status.releases] == [
        ("cert-manager", "cert-manager", "failed"),
        ("observability", "loki", "deployed"),
    ]
    assert status.urls == ("https://grafana.localhost/", "https://loki.localhost/")
    assert status.port_forward_pid == 4242


def test_status_reads_through_the_context_bound_clients(tmp_path: Path) -> None:
    """The report must address the kind cluster, not the ambient kubecontext.

    `up` rebinds Helm and kubectl to the resolved environment's context
    before it touches anything; a `status` that skipped that step would
    answer about whatever cluster the workstation's kubeconfig points at
    while claiming to describe the local lab.
    """
    _local_cluster(tmp_path)
    ambient = _Helm(
        releases=[ReleaseInfo(name="prod-app", namespace="prod", revision=9, status="deployed")]
    )
    bound = _Helm(
        releases=[ReleaseInfo(name="loki", namespace="observability", revision=1, status="deployed")]
    )
    contexts: list[str] = []

    def clients(handle: Any) -> BoundClients:
        contexts.append(handle.context)
        return BoundClients(
            helm=bound,  # type: ignore[arg-type]
            kubectl=_Kubectl(),  # type: ignore[arg-type]
            expose=_Expose(),  # type: ignore[arg-type]
        )

    status = _service(tmp_path, helm=ambient, client_factory=clients).status("chart-manager")

    assert contexts == ["kind-chart-manager"]
    assert [r.name for r in status.releases] == ["loki"]
    assert ambient.calls == 0


def test_status_captures_a_failed_release_listing_instead_of_raising(
    tmp_path: Path,
) -> None:
    _local_cluster(tmp_path)

    status = _service(
        tmp_path, helm=_Helm(raises=ExternalCommandError("no kubeconfig"))
    ).status("chart-manager")

    assert status.exists is True
    assert status.releases == ()
    assert status.releases_error is not None
    assert "no kubeconfig" in status.releases_error


def test_status_captures_a_failed_virtualservice_listing(tmp_path: Path) -> None:
    _local_cluster(tmp_path)

    status = _service(
        tmp_path, kubectl=_Kubectl(raises=ChartManagerError("istio not installed"))
    ).status("chart-manager")

    assert status.urls == ()
    assert status.urls_error is not None
    assert "istio not installed" in status.urls_error


def test_status_reports_port_mapping_drift_against_the_authored_kind_config(
    tmp_path: Path,
) -> None:
    """The baseline is the LocalCluster's `spec.cluster.config`, not a default.

    Pointing the drift check at `<root>/kind-config.yaml` regardless would
    report "no drift" for every repository that names its config anything
    else -- a check that cannot fail is not a check.
    """
    _local_cluster(tmp_path, host_ports=[80, 443])

    status = _service(tmp_path, kind=_Kind(host_ports={80})).status("chart-manager")

    assert status.drift.drifted is True
    assert status.drift.missing == (443,)


def test_status_survives_a_repository_with_no_local_cluster(tmp_path: Path) -> None:
    """A missing LocalCluster blocks `up`; it only removes status's baseline."""
    status = _service(tmp_path).status("chart-manager")

    assert status.exists is True
    assert status.drift.drifted is False
    assert status.drift.error is None


def test_status_payload_carries_the_documented_keys(tmp_path: Path) -> None:
    """The `jq '.releases[] | select(.status!="deployed")'` idiom, pinned."""
    _local_cluster(tmp_path)
    helm = _Helm(
        releases=[
            ReleaseInfo(name="loki", namespace="observability", revision=3, status="deployed")
        ]
    )

    payload = status_to_dict(_service(tmp_path, helm=helm).status("chart-manager"))

    assert payload["schema_version"] == 1
    assert payload["command"] == "status"
    assert payload["cluster_name"] == "chart-manager"
    # `ok` is existence, not health: status reports, it does not grade.
    assert payload["ok"] is True
    assert payload["releases"] == [
        {"name": "loki", "namespace": "observability", "revision": 3, "status": "deployed"}
    ]
    assert payload["drift"] == {"missing_host_ports": [], "error": None}
    assert payload["releases_error"] is None
    assert payload["urls_error"] is None


# ----- plan -----------------------------------------------------------------


def _target(root: Path, name: str) -> ResolvedChartTarget:
    return ResolvedChartTarget(name=name, path=(root / "charts" / name).resolve())


def test_plan_target_resolves_the_install_plan_without_touching_the_cluster(
    tmp_path: Path,
) -> None:
    """A dry run must be answerable with no cluster and no Docker daemon."""
    _local_cluster(tmp_path)
    _chart(tmp_path, "grafana", namespace="monitoring")
    kind = _Kind()

    plan = _service(tmp_path, kind=kind).plan_target(
        _target(tmp_path, "grafana"),
        profile="minimal",
        cluster_name="chart-manager",
    )

    assert plan.command == "up"
    assert plan.destroys is False
    assert plan.target == "grafana"
    assert plan.target_kind == "chart"
    assert [(e.source, e.chart, e.profile, e.namespace) for e in plan.entries] == [
        ("target", "grafana", "minimal", "monitoring")
    ]
    assert kind.ensure_calls == []


def test_plan_target_marks_reset_as_destructive(tmp_path: Path) -> None:
    _local_cluster(tmp_path)
    _chart(tmp_path, "grafana")

    plan = _service(tmp_path).plan_target(
        _target(tmp_path, "grafana"),
        profile="minimal",
        cluster_name="chart-manager",
        destroys=True,
    )

    assert plan.command == "reset"
    assert plan.destroys is True


def test_plan_target_fails_on_an_unresolvable_plan(tmp_path: Path) -> None:
    """The dry run runs the real preflight, so it rejects what the real run would."""
    _local_cluster(tmp_path)
    _chart(tmp_path, "grafana")

    with pytest.raises(ChartManagerError):
        _service(tmp_path).plan_target(
            _target(tmp_path, "grafana"),
            profile="does-not-exist",
            cluster_name="chart-manager",
        )


def test_plan_down_names_the_cluster_and_installs_nothing(tmp_path: Path) -> None:
    plan = _service(tmp_path).plan_down("chart-manager")

    assert plan.command == "down"
    assert plan.cluster_name == "chart-manager"
    assert plan.entries == ()
    assert plan.target is None
