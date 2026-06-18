"""Coverage for LabService.up / sync converge-by-default behavior.

Focus areas:
  * cilium does not appear twice in the install summary, even though it's
    a transitive dependency of grafana-dashboards:prototyping and would
    otherwise be visited by both the bootstrap branch and the install plan.
  * `up --skip-installed` restores the prior fast-skip path: charts already
    in `helm list -A` are reported as no-change and never invoke helm.
  * `sync <chart>` validates membership against the configured install
    plan; an unknown chart raises before any helm work runs.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import pytest
from rich.console import Console

from chart_manager.integrations.helm import ReleaseInfo, UpgradeResult
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.graph import PlanEntry
from chart_manager.plumbing.spec import ProfileSpec, TestSpec as _TestSpec
from chart_manager.plumbing.charts import Chart
from chart_manager.services import cluster_bootstrap, lab as lab_module
from chart_manager.services.lab import (
    LabService,
    LabSyncOptions,
    LabUpOptions,
)


class _RecordingHelm:
    """Helm fake that records every `helm upgrade --install` invocation.

    The class tests against summary buckets, not against what helm "would
    do" -- the no-change vs applied classification is decided here by
    `default_status`. Bootstrap and dependency-update calls are no-ops.
    """

    def __init__(
        self,
        *,
        releases: list[ReleaseInfo] | None = None,
        default_status: Literal["applied", "no-change"] = "applied",
    ) -> None:
        self._releases = releases or []
        self._default_status = default_status
        self.upgrade_calls: list[tuple[str, str]] = []
        self.dep_update_calls: list[Path] = []

    def list_releases(
        self,
        *,
        all_namespaces: bool = True,
        namespace: str | None = None,
    ) -> list[ReleaseInfo]:
        if all_namespaces:
            return list(self._releases)
        return [r for r in self._releases if namespace is None or r.namespace == namespace]

    def get_values(self, _release: str, *, namespace: str) -> dict[str, Any]:
        return {}

    def dependency_update_if_stale(self, path: Path) -> bool:
        self.dep_update_calls.append(path)
        return False

    def dependency_update(self, path: Path) -> None:
        self.dep_update_calls.append(path)

    def upgrade_install(
        self,
        release: str,
        chart_ref: Any,
        *,
        namespace: str,
        **_kwargs: Any,
    ) -> UpgradeResult:
        self.upgrade_calls.append((release, namespace))
        return UpgradeResult(
            status=self._default_status,
            revision_before=1,
            revision_after=1 if self._default_status == "no-change" else 2,
            output="",
        )

    def lint(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _FakeKind:
    def __init__(self, ip: str = "172.18.0.2") -> None:
        self._ip = ip

    def ensure_cluster(self, _name: str, *, config: Path | None = None) -> None:
        pass

    def control_plane_ip(self, _name: str) -> str:
        return self._ip

    def container_host_ports(self, _name: str) -> set[int]:
        return set()


class _FakeKubectl:
    def wait_apiserver_ready(self, *_args: Any, **_kwargs: Any) -> None:
        pass

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

    def get_secret_value(self, _name: str, _key: str, *, namespace: str) -> str:
        # Grafana access print path looks up the admin secret; the lab's
        # converge run reaches it whenever a grafana entry made it into
        # summary buckets. Returning a static string keeps the print path
        # quiet without requiring a kubectl runner.
        return "fake-password"


class _FakeExpose:
    def stop(self, _cluster_name: str) -> int | None:
        return None


def _service(tmp_path: Path, *, helm: _RecordingHelm, kind: _FakeKind) -> LabService:
    return LabService(
        tmp_path,
        helm=helm,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        kubectl=_FakeKubectl(),  # type: ignore[arg-type]
        expose=_FakeExpose(),  # type: ignore[arg-type]
        console=Console(quiet=True),
    )


def _stub_chart(name: str, *, namespace: str = "observability") -> Chart:
    """Synthesize an in-memory Chart with a minimal `minimal` profile.

    Skips disk I/O so LabService tests don't need a chart tree on tmp_path.
    The profile must specify the namespace explicitly because the lab
    resolver falls back to options.namespace otherwise -- which is fine
    for these tests but reads less clearly.
    """
    profile = ProfileSpec(
        description="stub",
        namespace=namespace,
        values=[],
        timeout="1m",
        requires=[],
        helm_test=False,
        checks=[],
    )
    spec = _TestSpec(profiles={"minimal": profile}, reverse_tests=[])
    return Chart(
        name=name,
        path=Path(f"/tmp/{name}"),
        chart_yaml={"name": name, "version": "0.0.0"},
        spec=spec,
    )


def _stub_plan_and_repo(
    monkeypatch: pytest.MonkeyPatch,
    service: LabService,
    *,
    plan: list[PlanEntry],
    charts: dict[str, Chart],
) -> None:
    """Replace the resolver + repository so tests don't need a chart tree.

    `plan` is what `install_plan` returns for any (chart, profile) tuple.
    `charts` is the lookup-by-name table backing `repository.get`. The
    `value_paths` repo method is stubbed to return [] (no overlay files)
    so `helm upgrade --install` is called with an empty values list.
    """
    monkeypatch.setattr(
        service.resolver, "install_plan", lambda _chart, _profile: list(plan)
    )

    def _get(name: str) -> Chart:
        if name not in charts:
            raise ChartManagerError(f"chart not found: {name}")
        return charts[name]

    monkeypatch.setattr(service.repository, "get", _get)
    monkeypatch.setattr(service.repository, "value_paths", lambda _c, _p: [])


def _disable_cilium_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force cluster_bootstrap.bootstrap to a no-op that reports applied.

    The real bootstrap reads cilium's chart on disk; these unit tests don't
    have one. We collapse it to "applied" so the lab summary path behaves
    as it would in production after a real bootstrap.
    """
    monkeypatch.setattr(
        lab_module.cluster_bootstrap,
        "bootstrap",
        lambda *_args, **_kwargs: "applied",
    )


# ----- duplicate-cilium-in-summary regression -------------------------------


def test_up_lists_cilium_once_even_when_plan_includes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # grafana-dashboards:prototyping transitively pulls cilium into the
    # install plan. Without the filter in LabService.up, cilium would
    # appear twice in the summary: once from bootstrap, once from the
    # plan-iteration loop.
    helm = _RecordingHelm(releases=[])
    kind = _FakeKind()
    service = _service(tmp_path, helm=helm, kind=kind)

    plan = [
        PlanEntry(chart="cilium", profile="minimal"),
        PlanEntry(chart="grafana", profile="minimal"),
    ]
    charts = {
        "cilium": _stub_chart("cilium", namespace="kube-system"),
        "grafana": _stub_chart("grafana", namespace="observability"),
    }
    _stub_plan_and_repo(monkeypatch, service, plan=plan, charts=charts)
    _disable_cilium_bootstrap(monkeypatch)

    summary: lab_module._RunSummary | None = None
    real_print = service._print_summary

    def _capture(s: lab_module._RunSummary) -> None:
        nonlocal summary
        summary = s
        real_print(s)

    monkeypatch.setattr(service, "_print_summary", _capture)

    service.up(LabUpOptions())

    assert summary is not None
    cilium_entries = [
        row
        for row in (*summary.applied, *summary.no_change)
        if row[0] == "cilium"
    ]
    assert len(cilium_entries) == 1, (
        f"cilium should appear once in summary; got {cilium_entries}"
    )
    # grafana should also have been applied -- proves the plan iteration
    # didn't get filtered too aggressively.
    grafana_entries = [
        row
        for row in (*summary.applied, *summary.no_change)
        if row[0] == "grafana"
    ]
    assert len(grafana_entries) == 1


# ----- skip-installed restores prior behavior -------------------------------


def test_up_skip_installed_does_not_invoke_helm_for_existing_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-existing release: with --skip-installed we should report
    # no-change and never call upgrade_install.
    helm = _RecordingHelm(
        releases=[
            ReleaseInfo(
                name="grafana", namespace="observability", revision=1, status="deployed"
            )
        ]
    )
    kind = _FakeKind()
    service = _service(tmp_path, helm=helm, kind=kind)

    plan = [PlanEntry(chart="grafana", profile="minimal")]
    charts = {"grafana": _stub_chart("grafana")}
    _stub_plan_and_repo(monkeypatch, service, plan=plan, charts=charts)
    _disable_cilium_bootstrap(monkeypatch)

    service.up(LabUpOptions(skip_installed=True))

    assert helm.upgrade_calls == [], (
        "skip_installed=True must not invoke helm for existing releases"
    )


def test_up_default_converges_existing_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same pre-existing release; default skip_installed=False should
    # converge it (call upgrade_install) so values edits land.
    helm = _RecordingHelm(
        releases=[
            ReleaseInfo(
                name="grafana", namespace="observability", revision=1, status="deployed"
            )
        ],
        default_status="no-change",
    )
    kind = _FakeKind()
    service = _service(tmp_path, helm=helm, kind=kind)

    plan = [PlanEntry(chart="grafana", profile="minimal")]
    charts = {"grafana": _stub_chart("grafana")}
    _stub_plan_and_repo(monkeypatch, service, plan=plan, charts=charts)
    _disable_cilium_bootstrap(monkeypatch)

    service.up(LabUpOptions())

    assert helm.upgrade_calls == [("grafana", "observability")], (
        "default must converge: every plan chart gets upgrade_install"
    )


# ----- sync verb ------------------------------------------------------------


def test_sync_runs_only_named_chart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helm = _RecordingHelm()
    kind = _FakeKind()
    service = _service(tmp_path, helm=helm, kind=kind)

    plan = [
        PlanEntry(chart="grafana", profile="minimal"),
        PlanEntry(chart="loki", profile="minimal"),
    ]
    charts = {
        "grafana": _stub_chart("grafana"),
        "loki": _stub_chart("loki"),
    }
    _stub_plan_and_repo(monkeypatch, service, plan=plan, charts=charts)
    _disable_cilium_bootstrap(monkeypatch)

    service.sync(LabSyncOptions(chart_names=("grafana",)))

    # Only grafana was named; loki must not be touched.
    assert helm.upgrade_calls == [("grafana", "observability")]


def test_sync_raises_when_chart_not_in_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helm = _RecordingHelm()
    kind = _FakeKind()
    service = _service(tmp_path, helm=helm, kind=kind)

    plan = [PlanEntry(chart="grafana", profile="minimal")]
    charts = {"grafana": _stub_chart("grafana")}
    _stub_plan_and_repo(monkeypatch, service, plan=plan, charts=charts)
    _disable_cilium_bootstrap(monkeypatch)

    with pytest.raises(ChartManagerError) as excinfo:
        service.sync(LabSyncOptions(chart_names=("does-not-exist",)))

    assert "does-not-exist" in str(excinfo.value)
    assert helm.upgrade_calls == [], "must not run helm when validation fails"


def test_sync_requires_at_least_one_chart_name(tmp_path: Path) -> None:
    helm = _RecordingHelm()
    kind = _FakeKind()
    service = _service(tmp_path, helm=helm, kind=kind)

    with pytest.raises(ChartManagerError):
        service.sync(LabSyncOptions(chart_names=()))
