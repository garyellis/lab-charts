"""DevelopmentClusterService access-hint resolution + cert/webhook gates + port-mapping drift.

Three concerns covered here:
  * `_access_hints`: empty / one / many VS results, with grafana
    credentials attached to the grafana URL but only if a grafana host is
    present. The service resolves the data; `cli/local.py` renders it (see
    test_lab_cli_rendering.py).
  * Cert + webhook waits: happy path (call recorded) and timeout path
    (warning surfaced, run continues).
  * Port-mapping drift: matching no-op, mismatch produces a warning event.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chart_manager.api.lifecycle.v1alpha1 import ClusterTestProfile
from chart_manager.api.lifecycle.v1alpha1 import ClusterTestSpec as _TestSpec
from chart_manager.api.local.v1alpha1 import LifecycleRelease, LocalCluster
from chart_manager.domain.charts import (
    ChartMetadata,
    ClusterTestChart,
    HelmChart,
)
from chart_manager.domain.install_plan import InstallPlanEntry
from chart_manager.domain.local_resources import ResolvedChartTarget
from chart_manager.integrations.helm import ReleaseInfo, UpgradeResult
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.clusters import development as lab_module
from chart_manager.services.clusters.bootstrap import LocalBootstrapExecutor
from chart_manager.services.clusters.development import (
    DevelopmentClusterEntryOutcome,
    DevelopmentClusterService,
)
from chart_manager.services.clusters.development.service import _TargetLocalExecution
from chart_manager.services.lifecycle.plan_projection import (
    ExternallySatisfiedLifecycle,
)
from chart_manager.services.progress import ProgressEvent

# Re-use the same shape of fakes the existing converge tests use; new
# behaviour gets new attributes (e.g. VS host list, port mapping set) and
# call counters where the test asserts on dispatch.


class _RecordingKubectl:
    def __init__(
        self,
        *,
        vs_hosts: list[str] | None = None,
        vs_raise: Exception | None = None,
        secret_raise: Exception | None = None,
        cert_raise: Exception | None = None,
        webhook_raise: Exception | None = None,
    ) -> None:
        self._vs_hosts = vs_hosts or []
        self._vs_raise = vs_raise
        self._secret_raise = secret_raise
        self._cert_raise = cert_raise
        self._webhook_raise = webhook_raise
        self.cert_waits: list[tuple[str, str, str]] = []
        self.webhook_waits: list[tuple[str, str, str]] = []
        self.secret_calls: list[tuple[str, str, str]] = []

    def wait_apiserver_ready(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def wait_workloads_ready(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def wait_certificate_ready(self, name: str, *, namespace: str, timeout: str = "120s") -> None:
        self.cert_waits.append((name, namespace, timeout))
        if self._cert_raise is not None:
            raise self._cert_raise

    def wait_deployment_available(
        self, name: str, *, namespace: str, timeout: str = "120s"
    ) -> None:
        self.webhook_waits.append((name, namespace, timeout))
        if self._webhook_raise is not None:
            raise self._webhook_raise

    def list_virtualservice_hosts(self) -> list[str]:
        if self._vs_raise is not None:
            raise self._vs_raise
        return list(self._vs_hosts)

    # Returns [] because this file's tests don't exercise the
    # gateway-host path; gateway-host-driven assertions live in
    # test_apps_domain_detection.py.
    def list_gateway_hosts(self) -> list[str]:
        return []

    def create_namespace(self, _namespace: str) -> None:
        pass

    def diagnostics(self, _namespace: str) -> str:
        return ""

    def get_secret_value(self, name: str, key: str, *, namespace: str) -> str:
        self.secret_calls.append((name, key, namespace))
        if self._secret_raise is not None:
            raise self._secret_raise
        return "fake-password"


class _Kind:
    def __init__(self, *, host_ports: set[int] | None = None) -> None:
        self._host_ports = host_ports if host_ports is not None else set()

    def ensure_cluster(self, _name: str, *, config: Path | None = None) -> None:
        pass

    def control_plane_ip(self, _name: str) -> str:
        return "172.18.0.2"

    def container_host_ports(self, _name: str) -> set[int]:
        return set(self._host_ports)


class _Helm:
    def __init__(self, *, status: str = "applied") -> None:
        self._status = status
        self.upgrade_calls: list[tuple[str, str]] = []

    def list_releases(
        self, *, all_namespaces: bool = True, namespace: str | None = None
    ) -> list[ReleaseInfo]:
        return []

    def get_values(self, _release: str, *, namespace: str) -> dict[str, Any]:
        return {}

    def dependency_update_if_stale(self, _path: Path) -> bool:
        return False

    def dependency_update(self, _path: Path) -> None:
        pass

    def upgrade_install(
        self, release: str, _chart: Any, *, namespace: str, **_kw: Any
    ) -> UpgradeResult:
        self.upgrade_calls.append((release, namespace))
        return UpgradeResult(
            status=self._status,
            revision_before=0,
            revision_after=1 if self._status == "applied" else 0,
            output="",
        )

    def lint(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _Expose:
    def stop(self, _cluster: str) -> int | None:
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
    helm: _Helm,
    kind: _Kind,
    kubectl: _RecordingKubectl,
    progress: _Recorder | None = None,
) -> DevelopmentClusterService:
    return DevelopmentClusterService(
        tmp_path,
        helm=helm,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        kubectl=kubectl,  # type: ignore[arg-type]
        expose=_Expose(),  # type: ignore[arg-type]
        progress=progress,
    )


def _stub_chart(name: str, *, namespace: str = "observability") -> ClusterTestChart:
    profile = ClusterTestProfile(
        description="stub",
        namespace=namespace,
        values=[],
        timeout="1m",
        requires=[],
        helmTest=False,
    )
    spec = _TestSpec(profiles={"minimal": profile}, dependentTests=[])
    return ClusterTestChart(
        chart=HelmChart(
            name=name,
            path=Path(f"/tmp/{name}"),
            metadata=ChartMetadata(name, "0.0.0", "application", ()),
        ),
        spec=spec,
    )


class _StubCatalog:
    """The two-method slice of ClusterTestCatalog that _install_plan reads."""

    def __init__(self, charts: dict[str, ClusterTestChart]) -> None:
        self._charts = charts

    def get(self, name: str) -> ClusterTestChart:
        return self._charts[name]

    def value_paths(self, _chart: ClusterTestChart, _profile: str) -> list[Path]:
        return []


def _install_plan(
    service: DevelopmentClusterService,
    plan: list[InstallPlanEntry],
    charts: dict[str, ClusterTestChart],
) -> lab_module.RunSummary:
    summary = lab_module.RunSummary()
    service._install_plan(
        plan,
        default_namespace="observability",
        installed_keys=set(),
        namespaces_created=set(),
        summary=summary,
        skip_installed=False,
        cluster_tests=_StubCatalog(charts),  # type: ignore[arg-type]
    )
    return summary


# ----- _access_hints --------------------------------------------------------


_GATEWAY_SYNCED = (DevelopmentClusterEntryOutcome("istio-gateway", "minimal", "istio-ingress"),)


def test_no_virtualservices_yields_no_urls(tmp_path: Path) -> None:
    # Empty VS list -> no URLs at all. The CA-trust decision still stands
    # because istio-gateway synced this run.
    kubectl = _RecordingKubectl(vs_hosts=[])
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)
    hints = svc._access_hints(
        lab_module.RunSummary(applied=list(_GATEWAY_SYNCED)),
        namespace="observability",
    )

    assert hints.urls == ()
    assert hints.grafana_url is None
    assert hints.ca_trust_hint is True
    assert kubectl.secret_calls == []


def test_single_virtualservice_yields_one_url_and_credentials(tmp_path: Path) -> None:
    kubectl = _RecordingKubectl(vs_hosts=["grafana.localhost"])
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)
    hints = svc._access_hints(
        lab_module.RunSummary(applied=list(_GATEWAY_SYNCED)),
        namespace="observability",
    )

    assert hints.urls == ("https://grafana.localhost/",)
    assert hints.grafana_url == "https://grafana.localhost/"
    # Grafana host -> credentials lookup must have fired
    assert kubectl.secret_calls == [("grafana", "admin-password", "observability")]
    assert hints.grafana_credentials == ("admin", "fake-password")
    assert hints.grafana_error is None


def test_many_virtualservices_yield_sorted_urls(tmp_path: Path) -> None:
    kubectl = _RecordingKubectl(vs_hosts=["prom.localhost", "grafana.localhost", "loki.localhost"])
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)
    hints = svc._access_hints(
        lab_module.RunSummary(applied=list(_GATEWAY_SYNCED)),
        namespace="observability",
    )

    # Hosts arrive in arbitrary order from kubectl; output is sorted.
    assert hints.urls == (
        "https://grafana.localhost/",
        "https://loki.localhost/",
        "https://prom.localhost/",
    )
    # Only grafana triggers the secret lookup
    assert kubectl.secret_calls == [("grafana", "admin-password", "observability")]


def test_ca_trust_hint_false_when_lab_ca_owner_absent(tmp_path: Path) -> None:
    # No istio-gateway in the summary -> CA decision is False, but the VS
    # list still resolves (a sync that touched only grafana).
    kubectl = _RecordingKubectl(vs_hosts=["grafana.localhost"])
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)
    hints = svc._access_hints(
        lab_module.RunSummary(
            applied=[DevelopmentClusterEntryOutcome("grafana", "minimal", "observability")]
        ),
        namespace="observability",
    )

    assert hints.ca_trust_hint is False
    assert hints.urls == ("https://grafana.localhost/",)


def test_virtualservice_listing_failure_is_captured_not_raised(tmp_path: Path) -> None:
    # Best-effort: a missing CRD must not abort the run, and the surface
    # needs the reason so it can render it where the URLs would have been.
    kubectl = _RecordingKubectl(vs_raise=ExternalCommandError("no such CRD"))
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)
    hints = svc._access_hints(
        lab_module.RunSummary(applied=list(_GATEWAY_SYNCED)),
        namespace="observability",
    )

    assert hints.urls == ()
    assert hints.urls_error is not None
    assert "could not list VirtualServices" in hints.urls_error
    assert hints.ca_trust_hint is True


def test_grafana_secret_failure_is_captured_not_raised(tmp_path: Path) -> None:
    kubectl = _RecordingKubectl(
        vs_hosts=["grafana.localhost"],
        secret_raise=ChartManagerError("secret not found"),
    )
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)
    hints = svc._access_hints(
        lab_module.RunSummary(applied=list(_GATEWAY_SYNCED)),
        namespace="observability",
    )

    assert hints.urls == ("https://grafana.localhost/",)
    assert hints.grafana_credentials is None
    assert hints.grafana_error == "secret not found"


# ----- _wait_apps_wildcard_ready --------------------------------------------


def test_apps_wildcard_wait_invoked_when_istio_gateway_in_summary(
    tmp_path: Path,
) -> None:
    kubectl = _RecordingKubectl()
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)
    summary = lab_module.RunSummary(no_change=list(_GATEWAY_SYNCED))
    svc._wait_apps_wildcard_ready(summary)

    assert kubectl.cert_waits == [("apps-wildcard", "istio-ingress", "120s")]


def test_apps_wildcard_wait_not_invoked_when_owner_chart_absent(
    tmp_path: Path,
) -> None:
    kubectl = _RecordingKubectl()
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)
    summary = lab_module.RunSummary(
        applied=[DevelopmentClusterEntryOutcome("grafana", "minimal", "observability")]
    )
    svc._wait_apps_wildcard_ready(summary)
    assert kubectl.cert_waits == []


def test_apps_wildcard_wait_timeout_is_warning_not_error(
    tmp_path: Path,
) -> None:
    # Best-effort: a cert wait that fails must not abort the print path.
    kubectl = _RecordingKubectl(
        cert_raise=ExternalCommandError("timed out waiting"),
    )
    progress = _Recorder()
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl, progress=progress)
    summary = lab_module.RunSummary(applied=list(_GATEWAY_SYNCED))
    svc._wait_apps_wildcard_ready(summary)
    assert "warn:" in progress.text
    assert "apps-wildcard cert not Ready" in progress.text


# ----- cert-manager webhook hook --------------------------------------------


def test_webhook_wait_runs_after_cert_manager_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cert-manager entry -> post-install hook -> wait_deployment_available
    # fires for `cert-manager-webhook` in `cert-manager`.
    kubectl = _RecordingKubectl()
    helm = _Helm(status="applied")
    svc = _service(tmp_path, helm=helm, kind=_Kind(), kubectl=kubectl)

    plan = [InstallPlanEntry(chart="cert-manager", profile="minimal")]
    charts = {"cert-manager": _stub_chart("cert-manager", namespace="cert-manager")}
    _install_plan(svc, plan, charts)

    assert kubectl.webhook_waits == [("cert-manager-webhook", "cert-manager", "120s")]


def test_webhook_wait_skipped_for_other_charts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kubectl = _RecordingKubectl()
    helm = _Helm(status="applied")
    svc = _service(tmp_path, helm=helm, kind=_Kind(), kubectl=kubectl)

    plan = [InstallPlanEntry(chart="grafana", profile="minimal")]
    charts = {"grafana": _stub_chart("grafana")}
    _install_plan(svc, plan, charts)
    assert kubectl.webhook_waits == []


def test_local_install_uses_the_chart_lifecycle_profile_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helm = _Helm(status="applied")
    svc = _service(
        tmp_path,
        helm=helm,
        kind=_Kind(),
        kubectl=_RecordingKubectl(),
    )
    summary = _install_plan(
        svc,
        [InstallPlanEntry(chart="grafana", profile="minimal")],
        {"grafana": _stub_chart("grafana", namespace="monitoring")},
    )

    assert helm.upgrade_calls == [("grafana", "monitoring")]
    assert summary.applied[0].namespace == "monitoring"


def test_target_preflight_excludes_bootstrap_owned_transitive_chart(
    tmp_path: Path,
) -> None:
    for name, requires in (("network", ""), ("app", "        requires:\n          - chart: network\n            profile: minimal\n")):
        chart = tmp_path / "charts" / name
        chart.mkdir(parents=True)
        (chart / "Chart.yaml").write_text(
            f"apiVersion: v2\nname: {name}\nversion: 1.0.0\n",
            encoding="utf-8",
        )
        (chart / "chart-lifecycle.yaml").write_text(
            (
                "apiVersion: lifecycle.chartmanager.io/v1alpha1\n"
                "kind: ChartLifecycle\n"
                f"metadata: {{name: {name}}}\n"
                "spec:\n"
                "  clusterTest:\n"
                "    profiles:\n"
                "      minimal:\n"
                f"{requires}"
                "        namespace: kube-system\n"
                "        values: []\n"
            ),
            encoding="utf-8",
        )
    svc = _service(
        tmp_path,
        helm=_Helm(),
        kind=_Kind(),
        kubectl=_RecordingKubectl(),
    )

    steps = svc._preflight_target(
        (
            LifecycleRelease(
                type="lifecycle",
                chart=Path("charts/app"),
                profile="minimal",
            ),
        ),
        excluded_lifecycle_identities=frozenset(
            {
                ExternallySatisfiedLifecycle(
                    chart_path=(tmp_path / "charts/network").resolve(),
                    chart="network",
                    profile="minimal",
                    namespace="kube-system",
                )
            }
        ),
    )

    assert isinstance(steps[0], _TargetLocalExecution)
    assert [entry.chart for entry in steps[0].plan] == ["app"]


def test_bootstrap_exclusion_survives_a_profile_that_declares_no_namespace(
    tmp_path: Path,
) -> None:
    """Both sides of the exclusion must fill an absent `namespace:` the same way.

    `LocalBootstrapExecutor.preflight` publishes ownership as an identity that
    *includes* the namespace, and `_preflight_target` excludes by exact
    identity. Those two used to spell the fallback as separate `"default"`
    literals, one per module; they now share `_shared.DEFAULT_NAMESPACE`. Fork
    them again and a bootstrap-owned chart is converged a second time -- with
    no error, because both installs succeed.
    """
    for name, requires in (
        ("network", ""),
        ("app", "        requires:\n          - chart: network\n            profile: minimal\n"),
    ):
        chart = tmp_path / "charts" / name
        chart.mkdir(parents=True)
        (chart / "Chart.yaml").write_text(
            f"apiVersion: v2\nname: {name}\nversion: 1.0.0\n",
            encoding="utf-8",
        )
        (chart / "chart-lifecycle.yaml").write_text(
            (
                "apiVersion: lifecycle.chartmanager.io/v1alpha1\n"
                "kind: ChartLifecycle\n"
                f"metadata: {{name: {name}}}\n"
                "spec:\n"
                "  clusterTest:\n"
                "    profiles:\n"
                "      minimal:\n"
                f"{requires}"
                "        values: []\n"
            ),
            encoding="utf-8",
        )
    bootstrap = LocalBootstrapExecutor(
        tmp_path,
        helm=_Helm(),  # type: ignore[arg-type]
        kind=_Kind(),  # type: ignore[arg-type]
        kubectl=_RecordingKubectl(),  # type: ignore[arg-type]
    )
    owned = bootstrap.preflight(
        LocalCluster.model_validate(
            {
                "apiVersion": "local.chartmanager.io/v1alpha1",
                "kind": "LocalCluster",
                "metadata": {"name": "default"},
                "spec": {
                    "cluster": {"config": "kind-config.yaml"},
                    "bootstrap": {
                        "releases": [
                            {
                                "type": "lifecycle",
                                "chart": "charts/network",
                                "profile": "minimal",
                            }
                        ]
                    },
                },
            }
        )
    )

    steps = _service(
        tmp_path,
        helm=_Helm(),
        kind=_Kind(),
        kubectl=_RecordingKubectl(),
    )._preflight_target(
        (LifecycleRelease(type="lifecycle", chart=Path("charts/app"), profile="minimal"),),
        excluded_lifecycle_identities=owned,
    )

    assert {identity.namespace for identity in owned} == {"default"}
    assert isinstance(steps[0], _TargetLocalExecution)
    assert [entry.chart for entry in steps[0].plan] == ["app"]


def test_relative_repository_root_accepts_an_absolute_resolved_chart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chart = tmp_path / "charts/cert-manager"
    chart.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    svc = _service(
        Path("."),
        helm=_Helm(),
        kind=_Kind(),
        kubectl=_RecordingKubectl(),
    )

    releases = svc._target_releases(
        ResolvedChartTarget(name="cert-manager", path=chart.resolve()),
        profile=None,
    )

    assert releases[0].chart == Path("charts/cert-manager")


def test_webhook_wait_warning_does_not_abort_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A webhook timeout warns and continues -- subsequent charts will
    # surface their own admission errors if the webhook truly isn't up.
    kubectl = _RecordingKubectl(
        webhook_raise=ExternalCommandError("timed out"),
    )
    helm = _Helm(status="applied")
    progress = _Recorder()
    svc = _service(tmp_path, helm=helm, kind=_Kind(), kubectl=kubectl, progress=progress)

    plan = [InstallPlanEntry(chart="cert-manager", profile="minimal")]
    charts = {"cert-manager": _stub_chart("cert-manager", namespace="cert-manager")}
    _install_plan(svc, plan, charts)
    assert "cert-manager webhook not Available" in progress.text


# ----- port-mapping drift ---------------------------------------------------


def test_port_mapping_drift_warning_when_live_missing_expected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kind-config declares 80 and 443; the live container reports only 80
    # -> drift; warn on 443.
    (tmp_path / "kind-config.yaml").write_text(
        "kind: Cluster\n"
        "apiVersion: kind.x-k8s.io/v1alpha4\n"
        "nodes:\n"
        "  - role: control-plane\n"
        "    extraPortMappings:\n"
        "      - containerPort: 30080\n"
        "        hostPort: 80\n"
        "      - containerPort: 30443\n"
        "        hostPort: 443\n",
    )
    kubectl = _RecordingKubectl()
    kind = _Kind(host_ports={80})
    helm = _Helm(status="applied")
    progress = _Recorder()
    svc = _service(tmp_path, helm=helm, kind=kind, kubectl=kubectl, progress=progress)

    svc._warn_on_port_mapping_drift(
        "chart-manager",
        config=tmp_path / "kind-config.yaml",
    )
    assert "kind cluster port mappings do not match kind-config" in progress.text
    assert "443" in progress.text


def test_port_mapping_drift_no_warning_when_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "kind-config.yaml").write_text(
        "kind: Cluster\n"
        "apiVersion: kind.x-k8s.io/v1alpha4\n"
        "nodes:\n"
        "  - role: control-plane\n"
        "    extraPortMappings:\n"
        "      - containerPort: 30080\n"
        "        hostPort: 80\n"
        "      - containerPort: 30443\n"
        "        hostPort: 443\n",
    )
    kubectl = _RecordingKubectl()
    kind = _Kind(host_ports={80, 443})
    helm = _Helm(status="applied")
    progress = _Recorder()
    svc = _service(tmp_path, helm=helm, kind=kind, kubectl=kubectl, progress=progress)
    svc._warn_on_port_mapping_drift(
        "chart-manager",
        config=tmp_path / "kind-config.yaml",
    )
    assert "kind cluster port mappings do not match" not in progress.text


def test_port_mapping_drift_silent_when_kind_config_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No kind-config.yaml in the repo root -> nothing to compare against,
    # so the check is a no-op. (Matches the sandbox-test path.)
    kubectl = _RecordingKubectl()
    kind = _Kind(host_ports=set())
    helm = _Helm(status="applied")
    progress = _Recorder()
    svc = _service(tmp_path, helm=helm, kind=kind, kubectl=kubectl, progress=progress)
    svc._warn_on_port_mapping_drift(
        "chart-manager",
        config=tmp_path / "kind-config.yaml",
    )
    assert "kind cluster port mappings" not in progress.text


def test_grafana_secret_is_read_from_the_namespace_grafana_landed_in(
    tmp_path: Path,
) -> None:
    """The lookup namespace comes from the summary, not the run default.

    A profile may declare its own `namespace:`. This read `options.namespace`
    instead, and the two coincide today only because the grafana cluster-test configuration
    omits one -- so adding that single line would have silently degraded the
    credential lookup to "secret not found" with no other symptom. The
    original test could not catch it: it passed the same value for both.
    """
    kubectl = _RecordingKubectl(vs_hosts=["grafana.localhost"])
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)

    hints = svc._access_hints(
        lab_module.RunSummary(
            applied=[
                *_GATEWAY_SYNCED,
                # Grafana installed into its own namespace, not the default.
                DevelopmentClusterEntryOutcome("grafana", "minimal", "monitoring"),
            ]
        ),
        namespace="observability",
    )

    assert kubectl.secret_calls == [("grafana", "admin-password", "monitoring")]
    assert hints.grafana_credentials == ("admin", "fake-password")


def test_grafana_secret_falls_back_to_the_run_namespace_when_absent(
    tmp_path: Path,
) -> None:
    """No grafana row in the summary -> keep the previous behavior."""
    kubectl = _RecordingKubectl(vs_hosts=["grafana.localhost"])
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)

    svc._access_hints(
        lab_module.RunSummary(applied=list(_GATEWAY_SYNCED)),
        namespace="observability",
    )

    assert kubectl.secret_calls == [("grafana", "admin-password", "observability")]
