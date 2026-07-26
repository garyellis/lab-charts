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

from chart_manager.integrations.helm import ReleaseInfo, UpgradeResult
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services import lab as lab_module
from chart_manager.services.domain.charts import (
    ChartMetadata,
    HelmChart,
    ManagedChart,
)
from chart_manager.services.domain.graph import PlanEntry
from chart_manager.services.domain.spec import ProfileSpec
from chart_manager.services.domain.spec import TestSpec as _TestSpec
from chart_manager.services.lab import (
    LabService,
    LabSyncOptions,
    LabUpOptions,
)
from chart_manager.services.progress import ProgressEvent


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


class _Recorder:
    """Collect progress events and flatten them to text for substring asserts."""

    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def __call__(self, event: ProgressEvent) -> None:
        self.events.append(event)

    @property
    def text(self) -> str:
        return "\n".join(f"{e.label or ''} {e.message}".strip() for e in self.events)


def _service(
    tmp_path: Path,
    *,
    helm: _RecordingHelm,
    kind: _FakeKind,
    progress: _Recorder | None = None,
) -> LabService:
    return LabService(
        tmp_path,
        helm=helm,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        kubectl=_FakeKubectl(),  # type: ignore[arg-type]
        expose=_FakeExpose(),  # type: ignore[arg-type]
        progress=progress,
    )


def _stub_chart(name: str, *, namespace: str = "observability") -> ManagedChart:
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
    return ManagedChart(
        chart=HelmChart(
            name=name,
            path=Path(f"/tmp/{name}"),
            metadata=ChartMetadata(name, "0.0.0", "application", ()),
        ),
        spec=spec,
    )


def _stub_plan_and_repo(
    monkeypatch: pytest.MonkeyPatch,
    service: LabService,
    *,
    plan: list[PlanEntry],
    charts: dict[str, ManagedChart],
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

    def _get(name: str) -> ManagedChart:
        if name not in charts:
            raise ChartManagerError(f"chart not found: {name}")
        return charts[name]

    monkeypatch.setattr(service.repository, "get_managed", _get)
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

    result = service.up(LabUpOptions())

    cilium_entries = [
        entry
        for entry in (*result.applied, *result.no_change)
        if entry.chart == "cilium"
    ]
    assert len(cilium_entries) == 1, (
        f"cilium should appear once in the result; got {cilium_entries}"
    )
    # grafana should also have been applied -- proves the plan iteration
    # didn't get filtered too aggressively.
    grafana_entries = [
        entry
        for entry in (*result.applied, *result.no_change)
        if entry.chart == "grafana"
    ]
    assert len(grafana_entries) == 1
    assert result.ok


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


# ----- down / delete return results -----------------------------------------


class _StoppableKind(_FakeKind):
    """Kind fake whose stop/delete verbs report a configurable outcome."""

    def __init__(self, *, changed: bool) -> None:
        super().__init__()
        self._changed = changed

    def stop_cluster(self, _name: str) -> bool:
        return self._changed

    def delete_cluster(self, _name: str) -> bool:
        return self._changed


class _ReapingExpose:
    def __init__(self, pid: int | None) -> None:
        self._pid = pid
        self.stop_calls: list[str] = []

    def stop(self, cluster_name: str) -> int | None:
        self.stop_calls.append(cluster_name)
        return self._pid


def _lifecycle_service(
    tmp_path: Path, *, kind: _StoppableKind, expose: _ReapingExpose
) -> LabService:
    return LabService(
        tmp_path,
        helm=_RecordingHelm(),  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        kubectl=_FakeKubectl(),  # type: ignore[arg-type]
        expose=expose,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("verb", ["down", "delete"])
def test_lifecycle_verb_reports_the_change_and_reaps_the_port_forward(
    tmp_path: Path, verb: str
) -> None:
    expose = _ReapingExpose(pid=4242)
    service = _lifecycle_service(
        tmp_path, kind=_StoppableKind(changed=True), expose=expose
    )

    result = getattr(service, verb)("chart-manager")

    assert result.ok
    assert result.cluster_name == "chart-manager"
    assert result.changed is True
    assert result.port_forward_pid == 4242
    # The port-forward is reaped inside the same lifecycle boundary; a
    # kubectl forward whose apiserver just went away is dead weight.
    assert expose.stop_calls == ["chart-manager"]


@pytest.mark.parametrize("verb", ["down", "delete"])
def test_lifecycle_verb_reports_an_absent_cluster_as_unchanged(
    tmp_path: Path, verb: str
) -> None:
    expose = _ReapingExpose(pid=None)
    service = _lifecycle_service(
        tmp_path, kind=_StoppableKind(changed=False), expose=expose
    )

    result = getattr(service, verb)("chart-manager")

    assert result.changed is False
    assert result.port_forward_pid is None


def test_lifecycle_verb_narrates_through_the_progress_callback(tmp_path: Path) -> None:
    progress = _Recorder()
    service = LabService(
        tmp_path,
        helm=_RecordingHelm(),  # type: ignore[arg-type]
        kind=_StoppableKind(changed=True),  # type: ignore[arg-type]
        kubectl=_FakeKubectl(),  # type: ignore[arg-type]
        expose=_ReapingExpose(pid=None),  # type: ignore[arg-type]
        progress=progress,
    )

    service.down("chart-manager")

    assert "Stopping sandbox cluster chart-manager" in progress.text


# ----- LabResult.ok ---------------------------------------------------------


def test_up_result_is_not_ok_when_a_chart_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A chart that isn't in the repository fails resolution; continue-on-error
    # means the run completes and the failure lands in the result.
    helm = _RecordingHelm()
    service = _service(tmp_path, helm=helm, kind=_FakeKind())
    plan = [
        PlanEntry(chart="missing", profile="minimal"),
        PlanEntry(chart="grafana", profile="minimal"),
    ]
    _stub_plan_and_repo(
        monkeypatch, service, plan=plan, charts={"grafana": _stub_chart("grafana")}
    )
    _disable_cilium_bootstrap(monkeypatch)

    result = service.up(LabUpOptions())

    assert not result.ok
    assert [f.chart for f in result.failed] == ["missing"]
    # Continue-on-error: the chart after the failure still converged.
    assert [e.chart for e in result.applied] == ["cilium", "grafana"]


def test_up_narration_goes_to_the_progress_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    progress = _Recorder()
    service = _service(
        tmp_path, helm=_RecordingHelm(), kind=_FakeKind(), progress=progress
    )
    _stub_plan_and_repo(
        monkeypatch,
        service,
        plan=[PlanEntry(chart="grafana", profile="minimal")],
        charts={"grafana": _stub_chart("grafana")},
    )
    _disable_cilium_bootstrap(monkeypatch)

    service.up(LabUpOptions())

    assert "Ensuring sandbox cluster chart-manager" in progress.text
    assert "Applying grafana:minimal -> observability" in progress.text
    assert "Waiting for workloads grafana" in progress.text


# ----- continue-on-error covers profile resolution too -----------------------


def test_an_unknown_profile_fails_one_row_instead_of_aborting_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`spec.profile()` raises SpecError, and it used to sit outside every try.

    A chart whose `requires:` names a renamed profile aborted the whole
    converge partway through -- contradicting the continue-on-error
    contract `_install_plan` documents, and leaving later charts
    uninstalled with no row explaining why.
    """
    helm = _RecordingHelm()
    service = _service(tmp_path, helm=helm, kind=_FakeKind())
    plan = [
        PlanEntry(chart="stale", profile="renamed-away"),
        PlanEntry(chart="grafana", profile="minimal"),
    ]
    _stub_plan_and_repo(
        monkeypatch,
        service,
        plan=plan,
        charts={
            # Has only a `minimal` profile, so "renamed-away" raises.
            "stale": _stub_chart("stale"),
            "grafana": _stub_chart("grafana"),
        },
    )
    _disable_cilium_bootstrap(monkeypatch)

    result = service.up(LabUpOptions())

    assert not result.ok
    assert [f.chart for f in result.failed] == ["stale"]
    assert "renamed-away" in result.failed[0].error
    # The run continued: the chart after the bad profile still converged.
    assert "grafana" in [e.chart for e in result.applied]
