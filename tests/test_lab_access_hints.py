"""LabService URL print path + cert/webhook gates + port-mapping drift.

Three concerns covered here:
  * `_print_virtualservice_urls`: empty / one / many VS results, with
    grafana credentials printed under the grafana URL but only if a
    grafana host is present.
  * Cert + webhook waits: happy path (call recorded) and timeout path
    (warning surfaced, run continues).
  * Port-mapping drift: matching no-op, mismatch produces a warning row.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from chart_manager.integrations.helm import ReleaseInfo, UpgradeResult
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.graph import PlanEntry
from chart_manager.plumbing.spec import ProfileSpec, TestSpec as _TestSpec
from chart_manager.plumbing.charts import Chart
from chart_manager.services import lab as lab_module
from chart_manager.services.lab import (
    LabService,
    LabUpOptions,
)


# Re-use the same shape of fakes the existing converge tests use; new
# behaviour gets new attributes (e.g. VS host list, port mapping set) and
# call counters where the test asserts on dispatch.


class _RecordingKubectl:
    def __init__(
        self,
        *,
        vs_hosts: list[str] | None = None,
        cert_raise: Exception | None = None,
        webhook_raise: Exception | None = None,
    ) -> None:
        self._vs_hosts = vs_hosts or []
        self._cert_raise = cert_raise
        self._webhook_raise = webhook_raise
        self.cert_waits: list[tuple[str, str, str]] = []
        self.webhook_waits: list[tuple[str, str, str]] = []
        self.secret_calls: list[tuple[str, str, str]] = []

    def wait_apiserver_ready(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def wait_workloads_ready(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def wait_certificate_ready(
        self, name: str, *, namespace: str, timeout: str = "120s"
    ) -> None:
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


def _service(
    tmp_path: Path,
    *,
    helm: _Helm,
    kind: _Kind,
    kubectl: _RecordingKubectl,
    console: Console | None = None,
) -> LabService:
    return LabService(
        tmp_path,
        helm=helm,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        kubectl=kubectl,  # type: ignore[arg-type]
        expose=_Expose(),  # type: ignore[arg-type]
        console=console or Console(quiet=True),
    )


def _stub_chart(name: str, *, namespace: str = "observability") -> Chart:
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


def _wire_repo(
    monkeypatch: pytest.MonkeyPatch,
    service: LabService,
    *,
    plan: list[PlanEntry],
    charts: dict[str, Chart],
) -> None:
    monkeypatch.setattr(
        service.resolver, "install_plan", lambda _c, _p: list(plan)
    )

    def _get(name: str) -> Chart:
        return charts[name]

    monkeypatch.setattr(service.repository, "get", _get)
    monkeypatch.setattr(service.repository, "value_paths", lambda _c, _p: [])


def _disable_cilium(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lab_module.cluster_bootstrap, "bootstrap", lambda *_a, **_k: "applied"
    )


# ----- _print_virtualservice_urls -------------------------------------------


def test_no_virtualservices_prints_nothing(tmp_path: Path) -> None:
    # Empty VS list -> no URLs block at all. The summary table still prints
    # but the access-hints function is silent.
    kubectl = _RecordingKubectl(vs_hosts=[])
    console = Console(record=True, width=200)
    svc = _service(
        tmp_path,
        helm=_Helm(),
        kind=_Kind(),
        kubectl=kubectl,
        console=console,
    )
    summary = lab_module._RunSummary(
        applied=[("istio-gateway", "minimal", "istio-ingress")]
    )
    svc._print_access_hints(summary, namespace="observability")

    out = console.export_text()
    assert "URLs:" not in out
    assert "https://" not in out
    assert kubectl.secret_calls == []


def test_single_virtualservice_prints_one_url(tmp_path: Path) -> None:
    kubectl = _RecordingKubectl(vs_hosts=["grafana.localhost"])
    console = Console(record=True, width=200)
    svc = _service(
        tmp_path,
        helm=_Helm(),
        kind=_Kind(),
        kubectl=kubectl,
        console=console,
    )
    summary = lab_module._RunSummary(
        applied=[("istio-gateway", "minimal", "istio-ingress")]
    )
    svc._print_access_hints(summary, namespace="observability")

    out = console.export_text()
    assert "https://grafana.localhost/" in out
    # Grafana host -> credentials lookup must have fired
    assert kubectl.secret_calls == [
        ("grafana", "admin-password", "observability")
    ]
    assert "user: admin" in out
    assert "fake-password" in out


def test_many_virtualservices_prints_sorted_unique_urls(tmp_path: Path) -> None:
    kubectl = _RecordingKubectl(
        vs_hosts=["prom.localhost", "grafana.localhost", "loki.localhost"]
    )
    console = Console(record=True, width=200)
    svc = _service(
        tmp_path,
        helm=_Helm(),
        kind=_Kind(),
        kubectl=kubectl,
        console=console,
    )
    summary = lab_module._RunSummary(
        applied=[("istio-gateway", "minimal", "istio-ingress")]
    )
    svc._print_access_hints(summary, namespace="observability")

    out = console.export_text()
    # Hosts arrive in arbitrary order from kubectl; output is sorted.
    pos_g = out.find("grafana.localhost")
    pos_l = out.find("loki.localhost")
    pos_p = out.find("prom.localhost")
    assert -1 < pos_g < pos_l < pos_p
    # Only grafana triggers the secret lookup
    assert kubectl.secret_calls == [
        ("grafana", "admin-password", "observability")
    ]


def test_virtualservice_urls_silent_when_lab_ca_absent(tmp_path: Path) -> None:
    # No istio-gateway in the summary -> CA hint skipped, but VS list is
    # still printed if hosts exist (a sync that touched only grafana).
    kubectl = _RecordingKubectl(vs_hosts=["grafana.localhost"])
    console = Console(record=True, width=200)
    svc = _service(
        tmp_path,
        helm=_Helm(),
        kind=_Kind(),
        kubectl=kubectl,
        console=console,
    )
    summary = lab_module._RunSummary(
        applied=[("grafana", "minimal", "observability")]
    )
    svc._print_access_hints(summary, namespace="observability")

    out = console.export_text()
    assert "Trust the lab CA" not in out
    assert "https://grafana.localhost/" in out


# ----- CA import hint platform gating ---------------------------------------


def test_ca_hint_includes_macos_one_liner_on_darwin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Darwin we surface the `security add-trusted-cert` one-liner so the
    # dev doesn't have to remember the keychain incantation.
    monkeypatch.setattr(lab_module.sys, "platform", "darwin")
    kubectl = _RecordingKubectl(vs_hosts=[])
    console = Console(record=True, width=200)
    svc = _service(
        tmp_path,
        helm=_Helm(),
        kind=_Kind(),
        kubectl=kubectl,
        console=console,
    )
    summary = lab_module._RunSummary(
        applied=[("istio-gateway", "minimal", "istio-ingress")]
    )
    svc._print_access_hints(summary, namespace="observability")

    out = console.export_text()
    assert "Trust the lab CA" in out
    assert "macOS one-liner" in out
    assert "security add-trusted-cert" in out


def test_ca_hint_omits_macos_one_liner_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On non-Darwin the `security add-trusted-cert` line is misleading
    # (the tool doesn't exist). The generic "import into your OS keychain"
    # line must still print so Linux devs aren't left without instruction.
    monkeypatch.setattr(lab_module.sys, "platform", "linux")
    kubectl = _RecordingKubectl(vs_hosts=[])
    console = Console(record=True, width=200)
    svc = _service(
        tmp_path,
        helm=_Helm(),
        kind=_Kind(),
        kubectl=kubectl,
        console=console,
    )
    summary = lab_module._RunSummary(
        applied=[("istio-gateway", "minimal", "istio-ingress")]
    )
    svc._print_access_hints(summary, namespace="observability")

    out = console.export_text()
    assert "Trust the lab CA" in out
    assert "import ~/lab-ca.crt into your OS keychain" in out
    assert "macOS one-liner" not in out
    assert "security add-trusted-cert" not in out


# ----- _wait_apps_wildcard_ready --------------------------------------------


def test_apps_wildcard_wait_invoked_when_istio_gateway_in_summary(
    tmp_path: Path,
) -> None:
    kubectl = _RecordingKubectl()
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)
    summary = lab_module._RunSummary(
        no_change=[("istio-gateway", "minimal", "istio-ingress")]
    )
    svc._wait_apps_wildcard_ready(summary)

    assert kubectl.cert_waits == [
        ("apps-wildcard", "istio-ingress", "120s")
    ]


def test_apps_wildcard_wait_not_invoked_when_owner_chart_absent(
    tmp_path: Path,
) -> None:
    kubectl = _RecordingKubectl()
    svc = _service(tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl)
    summary = lab_module._RunSummary(
        applied=[("grafana", "minimal", "observability")]
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
    console = Console(record=True, width=200)
    svc = _service(
        tmp_path, helm=_Helm(), kind=_Kind(), kubectl=kubectl, console=console
    )
    summary = lab_module._RunSummary(
        applied=[("istio-gateway", "minimal", "istio-ingress")]
    )
    svc._wait_apps_wildcard_ready(summary)
    out = console.export_text()
    assert "warn:" in out
    assert "apps-wildcard cert not Ready" in out


# ----- cert-manager webhook hook --------------------------------------------


def test_webhook_wait_runs_after_cert_manager_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cert-manager entry -> post-install hook -> wait_deployment_available
    # fires for `cert-manager-webhook` in `cert-manager`.
    kubectl = _RecordingKubectl()
    helm = _Helm(status="applied")
    svc = _service(tmp_path, helm=helm, kind=_Kind(), kubectl=kubectl)

    plan = [PlanEntry(chart="cert-manager", profile="minimal")]
    charts = {"cert-manager": _stub_chart("cert-manager", namespace="cert-manager")}
    _wire_repo(monkeypatch, svc, plan=plan, charts=charts)
    _disable_cilium(monkeypatch)

    svc.up(LabUpOptions())

    assert kubectl.webhook_waits == [
        ("cert-manager-webhook", "cert-manager", "120s")
    ]


def test_webhook_wait_skipped_for_other_charts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kubectl = _RecordingKubectl()
    helm = _Helm(status="applied")
    svc = _service(tmp_path, helm=helm, kind=_Kind(), kubectl=kubectl)

    plan = [PlanEntry(chart="grafana", profile="minimal")]
    charts = {"grafana": _stub_chart("grafana")}
    _wire_repo(monkeypatch, svc, plan=plan, charts=charts)
    _disable_cilium(monkeypatch)

    svc.up(LabUpOptions())
    assert kubectl.webhook_waits == []


def test_webhook_wait_warning_does_not_abort_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A webhook timeout warns and continues -- subsequent charts will
    # surface their own admission errors if the webhook truly isn't up.
    kubectl = _RecordingKubectl(
        webhook_raise=ExternalCommandError("timed out"),
    )
    helm = _Helm(status="applied")
    console = Console(record=True, width=200)
    svc = _service(
        tmp_path, helm=helm, kind=_Kind(), kubectl=kubectl, console=console
    )

    plan = [PlanEntry(chart="cert-manager", profile="minimal")]
    charts = {"cert-manager": _stub_chart("cert-manager", namespace="cert-manager")}
    _wire_repo(monkeypatch, svc, plan=plan, charts=charts)
    _disable_cilium(monkeypatch)

    svc.up(LabUpOptions())
    out = console.export_text()
    assert "cert-manager webhook not Available" in out


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
    console = Console(record=True, width=200)
    svc = _service(
        tmp_path, helm=helm, kind=kind, kubectl=kubectl, console=console
    )

    plan: list[PlanEntry] = []
    _wire_repo(monkeypatch, svc, plan=plan, charts={})
    _disable_cilium(monkeypatch)

    svc.up(LabUpOptions())
    out = console.export_text()
    assert "kind cluster port mappings do not match kind-config" in out
    assert "443" in out


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
    console = Console(record=True, width=200)
    svc = _service(
        tmp_path, helm=helm, kind=kind, kubectl=kubectl, console=console
    )
    _wire_repo(monkeypatch, svc, plan=[], charts={})
    _disable_cilium(monkeypatch)

    svc.up(LabUpOptions())
    out = console.export_text()
    assert "kind cluster port mappings do not match" not in out


def test_port_mapping_drift_silent_when_kind_config_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No kind-config.yaml in the repo root -> nothing to compare against,
    # so the check is a no-op. (Matches the sandbox-test path.)
    kubectl = _RecordingKubectl()
    kind = _Kind(host_ports=set())
    helm = _Helm(status="applied")
    console = Console(record=True, width=200)
    svc = _service(
        tmp_path, helm=helm, kind=kind, kubectl=kubectl, console=console
    )
    _wire_repo(monkeypatch, svc, plan=[], charts={})
    _disable_cilium(monkeypatch)

    svc.up(LabUpOptions())
    out = console.export_text()
    assert "kind cluster port mappings" not in out
