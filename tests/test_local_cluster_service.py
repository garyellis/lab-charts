"""Local ephemeral-cluster result accounting and environment setup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chart_manager.api.lifecycle.v1alpha1 import ClusterTestProfile
from chart_manager.api.lifecycle.v1alpha1 import ClusterTestSpec as _TestSpec
from chart_manager.integrations.helm import UpgradeResult
from chart_manager.plumbing.commands import CommandResult
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.clusters.ephemeral import (
    EphemeralTestClusterService,
    EphemeralTestRequest,
)
from chart_manager.services.domain.charts import (
    ChartMetadata,
    ClusterTestChart,
    HelmChart,
)
from chart_manager.services.domain.install_plan import InstallPlanEntry
from chart_manager.services.progress import ProgressEvent


class _Kind:
    def __init__(self) -> None:
        self.ensure_calls: list[tuple[str, Path | None]] = []

    def ensure_cluster(self, name: str, *, config: Path | None = None) -> None:
        self.ensure_calls.append((name, config))

    def control_plane_ip(self, _name: str) -> str:
        return "172.18.0.2"


class _Kubectl:
    def wait_apiserver_ready(self, *_a: Any, **_k: Any) -> None:
        pass

    def wait_workloads_ready(self, *_a: Any, **_k: Any) -> None:
        pass

    def create_namespace(self, _namespace: str) -> None:
        pass

    def diagnostics(self, _namespace: str) -> str:
        return ""


class _Helm:
    def __init__(self) -> None:
        self.test_calls: list[str] = []

    def dependency_update_if_stale(self, _path: Path) -> bool:
        return False

    def lint(self, *_a: Any, **_k: Any) -> None:
        pass

    def upgrade_install(self, *_a: Any, **_k: Any) -> UpgradeResult:
        return UpgradeResult(status="applied", revision_before=0, revision_after=1, output="")

    def test(self, release: str, *, namespace: str, timeout: str) -> CommandResult:
        self.test_calls.append(release)
        return CommandResult(args=("helm", "test"), returncode=0, stdout="", stderr="")


class _Recorder:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def __call__(self, event: ProgressEvent) -> None:
        self.events.append(event)

    @property
    def text(self) -> str:
        return "\n".join(f"{e.label or ''} {e.message}".strip() for e in self.events)


def _stub_chart(name: str, *, helm_test: bool = True) -> ClusterTestChart:
    profile = ClusterTestProfile(
        namespace="observability",
        values=[],
        timeout="1m",
        requires=[],
        helmTest=helm_test,
    )
    return ClusterTestChart(
        chart=HelmChart(
            name=name,
            path=Path(f"/tmp/{name}"),
            metadata=ChartMetadata(name, "0.0.0", "application", ()),
        ),
        spec=_TestSpec(profiles={"minimal": profile}, dependentTests=[]),
    )


def _local_cluster(tmp_path: Path) -> None:
    (tmp_path / "kind-config.yaml").write_text("kind: Cluster\n", encoding="utf-8")
    config = tmp_path / ".chart-manager/local-cluster.yaml"
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


def _service(
    tmp_path: Path,
    *,
    kind: _Kind,
    helm: _Helm,
    progress: _Recorder | None = None,
    configure: bool = True,
):
    if configure:
        _local_cluster(tmp_path)
    return EphemeralTestClusterService(
        tmp_path,
        helm=helm,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        kubectl=_Kubectl(),  # type: ignore[arg-type]
        progress=progress,
    )


# ----- ensure ---------------------------------------------------------------


def test_ensure_passes_the_repo_kind_config_when_present(tmp_path: Path) -> None:
    kind = _Kind()

    name = _service(tmp_path, kind=kind, helm=_Helm()).ensure_cluster("chart-manager")

    assert name == "chart-manager"
    assert kind.ensure_calls == [("chart-manager", tmp_path / "kind-config.yaml")]


def test_ensure_requires_local_cluster_resource(tmp_path: Path) -> None:
    with pytest.raises(ChartManagerError, match="local resource file does not exist"):
        _service(
            tmp_path,
            kind=_Kind(),
            helm=_Helm(),
            configure=False,
        ).ensure_cluster("chart-manager")


def test_ensure_narrates_through_the_progress_callback(tmp_path: Path) -> None:
    progress = _Recorder()

    _service(tmp_path, kind=_Kind(), helm=_Helm(), progress=progress).ensure_cluster("c")

    assert "Ensuring local cluster c" in progress.text


# ----- run result -----------------------------------------------------------


@pytest.fixture
def wired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[EphemeralTestClusterService, _Helm]:
    """An EphemeralTestClusterService with an in-memory plan and repository."""
    helm = _Helm()
    svc = _service(tmp_path, kind=_Kind(), helm=helm)
    charts = {"grafana": _stub_chart("grafana"), "loki": _stub_chart("loki")}
    monkeypatch.setattr(svc.cluster_tests, "get", lambda name: charts[name])
    monkeypatch.setattr(svc.cluster_tests, "value_paths", lambda _c, _p: [])
    monkeypatch.setattr(
        svc.resolver,
        "install_plan",
        lambda _c, _p: [
            InstallPlanEntry(chart="loki", profile="minimal"),
            InstallPlanEntry(chart="grafana", profile="minimal"),
        ],
    )
    return svc, helm


def test_run_returns_what_it_installed_and_tested(
    wired: tuple[EphemeralTestClusterService, _Helm],
) -> None:
    svc, helm = wired

    result = svc.run(EphemeralTestRequest(chart="grafana", ensure_cluster=False))

    assert result.ok
    assert result.chart == "grafana"
    assert result.installed == ("grafana", "loki")
    # `tested` is plan-ordered, unlike the deduped `installed` set.
    assert result.tested == ("loki", "grafana")
    assert result.namespaces == ("observability",)
    assert helm.test_calls == ["loki", "grafana"]


def test_run_omits_charts_whose_profile_disables_helm_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helm = _Helm()
    svc = _service(tmp_path, kind=_Kind(), helm=helm)
    monkeypatch.setattr(
        svc.cluster_tests, "get", lambda name: _stub_chart(name, helm_test=False)
    )
    monkeypatch.setattr(svc.cluster_tests, "value_paths", lambda _c, _p: [])
    monkeypatch.setattr(
        svc.resolver,
        "install_plan",
        lambda _c, _p: [InstallPlanEntry(chart="grafana", profile="minimal")],
    )
    result = svc.run(EphemeralTestRequest(chart="grafana", ensure_cluster=False))

    assert result.installed == ("grafana",)
    assert result.tested == ()
    assert helm.test_calls == []


def test_run_ensures_the_cluster_and_narrates_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kind = _Kind()
    progress = _Recorder()
    svc = _service(tmp_path, kind=kind, helm=_Helm(), progress=progress)
    monkeypatch.setattr(svc, "_compile_lifecycle_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(svc, "_execute_lifecycle_plan", lambda *_args, **_kwargs: None)

    # Chart execution is covered separately; this test isolates environment
    # ensuring and its user-facing narration.
    svc.run(EphemeralTestRequest(chart="grafana", cluster_name="sbx"))

    assert kind.ensure_calls == [("sbx", tmp_path / "kind-config.yaml")]
    assert "Ensuring local cluster sbx" in progress.text
    assert "Waiting for kube-apiserver" in progress.text


def test_run_preflights_the_complete_plan_before_cluster_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kind = _Kind()
    svc = _service(tmp_path, kind=kind, helm=_Helm())

    def reject_plan(*_args: Any, **_kwargs: Any) -> None:
        raise ChartManagerError("later release is malformed")

    monkeypatch.setattr(svc, "_compile_lifecycle_plan", reject_plan)

    with pytest.raises(ChartManagerError, match="later release is malformed"):
        svc.run(EphemeralTestRequest(chart="grafana", cluster_name="sbx"))

    assert kind.ensure_calls == []
