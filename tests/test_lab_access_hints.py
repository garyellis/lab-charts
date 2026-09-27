"""DevelopmentClusterService access-hint resolution + cert/webhook gates + port-mapping drift.

Three concerns covered here:
  * `_access_hints`: empty / one / many VS results, with credentials
    attached to the URLs of every VirtualService that opts in through the
    `chartmanager.io/credentials-*` annotations, read from that
    VirtualService's own namespace. The service resolves the data;
    `cli/local.py` renders it (see test_lab_cli_rendering.py).
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
from chart_manager.domain.charts import (
    ChartMetadata,
    ClusterTestChart,
    HelmChart,
)
from chart_manager.domain.install_plan import InstallPlanEntry
from chart_manager.integrations.helm import ReleaseInfo, UpgradeResult
from chart_manager.integrations.kubectl import VirtualService
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.clusters import development as lab_module
from chart_manager.services.clusters.development import (
    DevelopmentClusterCredentials,
    DevelopmentClusterEntryOutcome,
    DevelopmentClusterService,
)
from chart_manager.services.progress import ProgressEvent

# Re-use the same shape of fakes the existing converge tests use; new
# behaviour gets new attributes (e.g. VS host list, port mapping set) and
# call counters where the test asserts on dispatch.


class _RecordingKubectl:
    def __init__(
        self,
        *,
        virtualservices: list[VirtualService] | None = None,
        vs_raise: Exception | None = None,
        secret_raise: Exception | None = None,
        cert_raise: Exception | None = None,
        webhook_raise: Exception | None = None,
    ) -> None:
        self._virtualservices = virtualservices or []
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

    def list_virtualservices(self) -> list[VirtualService]:
        if self._vs_raise is not None:
            raise self._vs_raise
        return list(self._virtualservices)

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
        installed_keys=set(),
        namespaces_created=set(),
        summary=summary,
        skip_installed=False,
        cluster_tests=_StubCatalog(charts),  # type: ignore[arg-type]
    )
    return summary


# ----- _access_hints --------------------------------------------------------


_GATEWAY_SYNCED = (DevelopmentClusterEntryOutcome("istio-gateway", "minimal", "istio-ingress"),)
_CREDENTIAL_ANNOTATIONS = {
    "chartmanager.io/credentials-secret": "app-admin",
    "chartmanager.io/credentials-username": "admin",
    "chartmanager.io/credentials-password-key": "password",
}


def _vs(
    *hosts: str, namespace: str = "apps", annotations: dict[str, str] | None = None
) -> VirtualService:
    return VirtualService(namespace=namespace, hosts=hosts, annotations=annotations or {})


def _hints(tmp_path: Path, kubectl: _RecordingKubectl) -> lab_module.DevelopmentClusterAccessHints:
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)
    return svc._access_hints(lab_module.RunSummary(applied=list(_GATEWAY_SYNCED)))


def test_no_virtualservices_yields_no_urls(tmp_path: Path) -> None:
    # Empty VS list -> no URLs at all. The CA-trust decision still stands
    # because istio-gateway synced this run.
    hints = _hints(tmp_path, _RecordingKubectl(virtualservices=[]))

    assert hints.urls == ()
    assert hints.ca_trust_hint is True


def test_unannotated_virtualservice_yields_url_only(tmp_path: Path) -> None:
    kubectl = _RecordingKubectl(virtualservices=[_vs("app.localhost")])
    hints = _hints(tmp_path, kubectl)

    assert hints.urls == ("https://app.localhost/",)
    assert hints.credentials == ()
    assert kubectl.secret_calls == []


def test_annotated_virtualservices_read_credentials_from_their_own_namespace(
    tmp_path: Path,
) -> None:
    # The Secret is read where each VirtualService lives, once per
    # VirtualService, and attached to every one of its URLs.
    kubectl = _RecordingKubectl(
        virtualservices=[
            _vs("prom.localhost", namespace="metrics"),
            _vs("b.localhost", namespace="ns-b", annotations=_CREDENTIAL_ANNOTATIONS),
            _vs(
                "a.localhost",
                "a.alt.localhost",
                namespace="ns-a",
                annotations=_CREDENTIAL_ANNOTATIONS,
            ),
        ]
    )
    hints = _hints(tmp_path, kubectl)

    # Hosts arrive in arbitrary order from kubectl; output is sorted.
    assert hints.urls == (
        "https://a.alt.localhost/",
        "https://a.localhost/",
        "https://b.localhost/",
        "https://prom.localhost/",
    )
    assert sorted(kubectl.secret_calls) == [
        ("app-admin", "password", "ns-a"),
        ("app-admin", "password", "ns-b"),
    ]
    assert hints.credentials == tuple(
        DevelopmentClusterCredentials(url=url, username="admin", password="fake-password")
        for url in hints.urls[:3]
    )


def test_a_host_claimed_twice_keeps_the_first_virtualservices_credentials(
    tmp_path: Path,
) -> None:
    failing = {**_CREDENTIAL_ANNOTATIONS, "chartmanager.io/credentials-username": ""}
    kubectl = _RecordingKubectl(
        virtualservices=[
            _vs("app.localhost", namespace="first", annotations=_CREDENTIAL_ANNOTATIONS),
            _vs("app.localhost", namespace="second", annotations=failing),
        ]
    )
    hints = _hints(tmp_path, kubectl)

    assert hints.credentials == (
        DevelopmentClusterCredentials(
            url="https://app.localhost/", username="admin", password="fake-password"
        ),
    )


def test_empty_credentials_secret_annotation_is_not_an_opt_in(tmp_path: Path) -> None:
    annotations = {**_CREDENTIAL_ANNOTATIONS, "chartmanager.io/credentials-secret": ""}
    kubectl = _RecordingKubectl(virtualservices=[_vs("app.localhost", annotations=annotations)])
    hints = _hints(tmp_path, kubectl)

    assert hints.credentials == ()
    assert kubectl.secret_calls == []


@pytest.mark.parametrize(
    "missing",
    ["chartmanager.io/credentials-username", "chartmanager.io/credentials-password-key"],
)
def test_incomplete_annotations_warn_without_reading_the_secret(
    tmp_path: Path, missing: str
) -> None:
    annotations = {k: v for k, v in _CREDENTIAL_ANNOTATIONS.items() if k != missing}
    kubectl = _RecordingKubectl(virtualservices=[_vs("app.localhost", annotations=annotations)])
    hints = _hints(tmp_path, kubectl)

    assert kubectl.secret_calls == []
    [credentials] = hints.credentials
    assert credentials.url == "https://app.localhost/"
    assert credentials.password is None
    assert credentials.error is not None
    assert missing in credentials.error


def test_secret_read_failure_is_captured_not_raised(tmp_path: Path) -> None:
    kubectl = _RecordingKubectl(
        virtualservices=[_vs("app.localhost", annotations=_CREDENTIAL_ANNOTATIONS)],
        secret_raise=ChartManagerError("secret not found"),
    )
    hints = _hints(tmp_path, kubectl)

    assert hints.urls == ("https://app.localhost/",)
    assert hints.credentials == (
        DevelopmentClusterCredentials(url="https://app.localhost/", error="secret not found"),
    )


def test_ca_trust_hint_false_when_lab_ca_owner_absent(tmp_path: Path) -> None:
    # No istio-gateway in the summary -> CA decision is False, but the VS
    # list still resolves (a sync that touched only some app chart).
    kubectl = _RecordingKubectl(virtualservices=[_vs("app.localhost")])
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)
    hints = svc._access_hints(
        lab_module.RunSummary(applied=[DevelopmentClusterEntryOutcome("app", "minimal", "apps")])
    )

    assert hints.ca_trust_hint is False
    assert hints.urls == ("https://app.localhost/",)


def test_virtualservice_listing_failure_is_captured_not_raised(tmp_path: Path) -> None:
    # Best-effort: a missing CRD must not abort the run, and the surface
    # needs the reason so it can render it where the URLs would have been.
    kubectl = _RecordingKubectl(vs_raise=ExternalCommandError("no such CRD"))
    hints = _hints(tmp_path, kubectl)

    assert hints.urls == ()
    assert hints.urls_error is not None
    assert "could not list VirtualServices" in hints.urls_error
    assert hints.ca_trust_hint is True


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


def test_webhook_wait_runs_after_cert_manager_apply(tmp_path: Path) -> None:
    # cert-manager entry -> post-install hook -> wait_deployment_available
    # fires for `cert-manager-webhook` in `cert-manager`.
    kubectl = _RecordingKubectl()
    helm = _Helm(status="applied")
    svc = _service(tmp_path, helm=helm, kind=_Kind(), kubectl=kubectl)

    plan = [InstallPlanEntry(chart="cert-manager", profile="minimal")]
    charts = {"cert-manager": _stub_chart("cert-manager", namespace="cert-manager")}
    _install_plan(svc, plan, charts)

    assert kubectl.webhook_waits == [("cert-manager-webhook", "cert-manager", "120s")]


def test_webhook_wait_skipped_for_other_charts(tmp_path: Path) -> None:
    kubectl = _RecordingKubectl()
    helm = _Helm(status="applied")
    svc = _service(tmp_path, helm=helm, kind=_Kind(), kubectl=kubectl)

    plan = [InstallPlanEntry(chart="grafana", profile="minimal")]
    charts = {"grafana": _stub_chart("grafana")}
    _install_plan(svc, plan, charts)
    assert kubectl.webhook_waits == []


def test_webhook_wait_warning_does_not_abort_run(tmp_path: Path) -> None:
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


def test_port_mapping_drift_warning_when_live_missing_expected(tmp_path: Path) -> None:
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


def test_port_mapping_drift_no_warning_when_matching(tmp_path: Path) -> None:
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


def test_port_mapping_drift_without_kind_config_is_silent_but_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """"Could not compare" and "no drift" are the same `PortMappingDrift` value.

    With nothing to compare against, the check returns `PortMappingDrift()`
    -- `missing=()` and `error=None` -- which is byte-for-byte what a clean
    cluster returns. Narration is deliberately silent (there is nothing to tell
    a developer to do), so the log is the only place the distinction survives,
    and it is what keeps a typo'd `spec.cluster.config` from disabling the
    check permanently with no signal at all.
    """
    progress = _Recorder()
    svc = _service(
        tmp_path,
        helm=_Helm(status="applied"),
        kind=_Kind(host_ports=set()),
        kubectl=_RecordingKubectl(),
        progress=progress,
    )

    with caplog.at_level("WARNING"):
        svc._warn_on_port_mapping_drift(
            "chart-manager",
            config=tmp_path / "kind-config.yaml",
        )

    assert "kind cluster port mappings" not in progress.text
    [record] = [r for r in caplog.records if r.levelname == "WARNING"]
    assert "port-mapping drift check skipped" in record.getMessage()
    assert "cluster=chart-manager" in record.getMessage()
