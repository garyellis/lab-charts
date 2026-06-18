"""Cilium k8sServiceHost drift detection in `LabService.up`.

If the docker `kind` network is recreated (e.g. `docker network prune`),
the control-plane container's IP changes but the installed cilium release
still pins the old `k8sServiceHost`. We detect this and bail with a clear
recovery message rather than silently leaving the cluster in a broken
state where kube-proxy-replacement traffic goes to the wrong IP.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from chart_manager.integrations.helm import ReleaseInfo, UpgradeResult
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services import lab as lab_module
from chart_manager.services.lab import LabService, LabUpOptions


class _FakeKind:
    def __init__(self, ip: str) -> None:
        self._ip = ip
        self.ensure_calls: list[str] = []

    def ensure_cluster(self, name: str, *, config: Path | None = None) -> None:
        self.ensure_calls.append(name)

    def control_plane_ip(self, name: str) -> str:
        return self._ip

    def container_host_ports(self, _name: str) -> set[int]:
        return set()


class _FakeHelm:
    def __init__(
        self,
        *,
        releases: list[ReleaseInfo],
        values: dict[str, Any] | Exception,
    ) -> None:
        self._releases = releases
        self._values = values
        self.upgrade_calls: list[tuple[str, str]] = []
        self.drift_must_block_install = False

    def list_releases(
        self,
        *,
        all_namespaces: bool = True,
        namespace: str | None = None,
    ) -> list[ReleaseInfo]:
        if all_namespaces:
            return self._releases
        return [r for r in self._releases if namespace is None or r.namespace == namespace]

    def get_values(self, release: str, *, namespace: str) -> dict[str, Any]:
        if isinstance(self._values, Exception):
            raise self._values
        return self._values

    def dependency_update_if_stale(self, _path: Path) -> bool:
        if self.drift_must_block_install:  # pragma: no cover - assertion path
            raise AssertionError("install path must not run when drift fires")
        return False

    def dependency_update(self, _path: Path) -> None:  # legacy fallback
        if self.drift_must_block_install:  # pragma: no cover - assertion path
            raise AssertionError("install path must not run when drift fires")

    def upgrade_install(self, *_args: Any, **_kwargs: Any) -> UpgradeResult:
        if self.drift_must_block_install:  # pragma: no cover - assertion path
            raise AssertionError("install path must not run when drift fires")
        # Simulate "no-change" on a converge: pre-existing release, same
        # rendered manifests, same chart version. Tests that need the
        # rollout-wait path can flip this per-instance.
        return UpgradeResult(
            status="no-change",
            revision_before=1,
            revision_after=1,
            output="",
        )


class _FakeKubectl:
    def __init__(self) -> None:
        self.ready_calls = 0

    def wait_apiserver_ready(self, *_args: Any, **_kwargs: Any) -> None:
        self.ready_calls += 1

    def wait_workloads_ready(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def wait_certificate_ready(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def wait_deployment_available(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def list_virtualservice_hosts(self) -> list[str]:
        return []

    def list_gateway_hosts(self) -> list[str]:
        return []

    def create_namespace(self, _namespace: str) -> None:
        pass

    def diagnostics(self, _namespace: str) -> str:
        return ""


class _FakeExpose:
    def stop(self, _cluster_name: str) -> int | None:
        return None


def _service(
    tmp_path: Path,
    *,
    helm: _FakeHelm,
    kind: _FakeKind,
) -> LabService:
    return LabService(
        tmp_path,
        helm=helm,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        kubectl=_FakeKubectl(),  # type: ignore[arg-type]
        expose=_FakeExpose(),  # type: ignore[arg-type]
        console=Console(quiet=True),
    )


def _stub_empty_plan(monkeypatch: pytest.MonkeyPatch, service: LabService) -> None:
    """Force the install plan to be empty so positive drift cases can run
    `LabService.up` to completion and assert it raised nothing.

    Without this, the resolver fails on a tmp_path-rooted repo (no charts
    on disk) and tests have to assert against the *absence* of the drift
    string in an unrelated downstream error -- brittle.
    """
    monkeypatch.setattr(
        service.resolver, "install_plan", lambda _chart, _profile: []
    )


def _cilium_installed() -> list[ReleaseInfo]:
    return [
        ReleaseInfo(
            name=lab_module.CILIUM_BOOTSTRAP_CHART,
            namespace=lab_module.CILIUM_BOOTSTRAP_NAMESPACE,
            revision=1,
            status="deployed",
        )
    ]


def test_up_raises_on_cilium_service_host_drift(tmp_path: Path) -> None:
    helm = _FakeHelm(
        releases=_cilium_installed(),
        values={"cilium": {"k8sServiceHost": "172.18.0.2"}},
    )
    helm.drift_must_block_install = True
    kind = _FakeKind(ip="172.20.0.5")  # docker `kind` network was recreated
    service = _service(tmp_path, helm=helm, kind=kind)

    with pytest.raises(ChartManagerError) as excinfo:
        service.up(LabUpOptions())

    msg = str(excinfo.value)
    assert "172.18.0.2" in msg
    assert "172.20.0.5" in msg
    assert "sandbox-delete" in msg


def test_up_passes_when_cilium_service_host_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same IP: no drift. up() must run cleanly to completion (no exception)
    # once the install plan is stubbed empty.
    helm = _FakeHelm(
        releases=_cilium_installed(),
        values={"cilium": {"k8sServiceHost": "172.18.0.2"}},
    )
    kind = _FakeKind(ip="172.18.0.2")
    service = _service(tmp_path, helm=helm, kind=kind)
    _stub_empty_plan(monkeypatch, service)

    service.up(LabUpOptions())  # no exception => drift check passed


def test_up_warns_and_continues_when_helm_get_values_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `helm get values` failure (e.g. transient) must NOT block the run.
    helm = _FakeHelm(
        releases=_cilium_installed(),
        values=ExternalCommandError("transient"),
    )
    kind = _FakeKind(ip="172.18.0.2")
    service = _service(tmp_path, helm=helm, kind=kind)
    _stub_empty_plan(monkeypatch, service)

    service.up(LabUpOptions())  # warn-and-continue => no exception


def test_up_warns_and_continues_when_service_host_key_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Older cilium installs predate the k8sServiceHost values key; the
    # absence is benign, not a hard failure.
    helm = _FakeHelm(
        releases=_cilium_installed(),
        values={"cilium": {"someOtherKey": "x"}},
    )
    kind = _FakeKind(ip="172.18.0.2")
    service = _service(tmp_path, helm=helm, kind=kind)
    _stub_empty_plan(monkeypatch, service)

    service.up(LabUpOptions())  # warn-and-continue => no exception
