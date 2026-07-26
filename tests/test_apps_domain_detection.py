"""Apps-domain detection + URL resolution owned by `ExposeService`.

The service queries `kubectl get gateway -A`, strips `*.` from each host,
and picks the most-common suffix as the apps-domain. Fallback to
`localhost` when no Gateway is installed yet (pre-lab or sandbox-test
path). The behaviour must be deterministic on ties so the printed URL is
reproducible across runs.

Lived in `cli/main.py` until the rule moved onto the service; the CLI now
only prints `ExposeStatus.urls`.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.expose import (
    APPS_DOMAIN_FALLBACK,
    DEFAULT_PORTS,
    ExposeRequest,
    ExposeService,
)


class _StubKubectl:
    def __init__(self, hosts: list[str] | None = None, *, raises: bool = False) -> None:
        self._hosts = hosts or []
        self._raises = raises
        # Unpinned, like a `Kubectl()` built with no context: the service
        # must then fall back to kind's context naming for the named cluster.
        self.context: str | None = None

    def list_gateway_hosts(self) -> list[str]:
        if self._raises:
            raise ChartManagerError("gateway CRD not installed")
        return list(self._hosts)


def _service(kubectl: _StubKubectl, tmp_path: Path) -> ExposeService:
    return ExposeService(state_dir=tmp_path, kubectl=kubectl)  # type: ignore[arg-type]


def test_detect_apps_domain_returns_fallback_when_no_gateway(tmp_path: Path) -> None:
    assert _service(_StubKubectl([]), tmp_path).apps_domain() == APPS_DOMAIN_FALLBACK


def test_detect_apps_domain_returns_fallback_when_kubectl_fails(tmp_path: Path) -> None:
    # A missing Gateway CRD is the pre-istio case, not an error worth
    # aborting the URL print for.
    svc = _service(_StubKubectl(raises=True), tmp_path)
    assert svc.apps_domain() == APPS_DOMAIN_FALLBACK


def test_detect_apps_domain_strips_wildcard_prefix(tmp_path: Path) -> None:
    assert _service(_StubKubectl(["*.localhost"]), tmp_path).apps_domain() == "localhost"


def test_detect_apps_domain_picks_most_common_suffix(tmp_path: Path) -> None:
    hosts = [
        "*.localhost",
        "*.localhost",
        "*.k8s.home.lab.io",
    ]
    assert _service(_StubKubectl(hosts), tmp_path).apps_domain() == "localhost"


def test_detect_apps_domain_breaks_ties_alphabetically(tmp_path: Path) -> None:
    # Two suffixes each appear once -- alphabetical pick: kind.local <
    # localhost, so kind.local wins regardless of input order.
    hosts = ["*.localhost", "*.kind.local"]
    assert _service(_StubKubectl(hosts), tmp_path).apps_domain() == "kind.local"


def test_detect_apps_domain_handles_bare_host_without_wildcard(tmp_path: Path) -> None:
    # A non-wildcard host (e.g. a one-off explicit host) is used as-is.
    hosts = ["foo.kind.local", "*.kind.local", "*.kind.local"]
    assert _service(_StubKubectl(hosts), tmp_path).apps_domain() == "kind.local"


# ----- port defaults + scheme heuristic -------------------------------------


def test_empty_port_list_falls_back_to_the_lab_default_map() -> None:
    request = ExposeRequest(cluster_name="c", service="ns/name", ports=[])
    assert request.ports == list(DEFAULT_PORTS)


def test_explicit_ports_are_preserved() -> None:
    request = ExposeRequest(cluster_name="c", service="ns/name", ports=["9000:80"])
    assert request.ports == ["9000:80"]


def test_omitted_ports_default_to_the_lab_map() -> None:
    assert ExposeRequest(cluster_name="c", service="ns/name").ports == list(DEFAULT_PORTS)


def test_https_scheme_only_for_well_known_tls_remote_ports() -> None:
    from chart_manager.services.expose import _resolve_urls

    urls = _resolve_urls(["8443:443", "8080:80", "9000:8443", "7000:8081"], "kind.local")
    assert [u.url for u in urls] == [
        "https://*.kind.local:8443/",
        "http://*.kind.local:8080/",
        "https://*.kind.local:9000/",
        "http://*.kind.local:7000/",
    ]
    assert [u.remote_port for u in urls] == ["443", "80", "8443", "8081"]


# ----- ExposeStatus carries the resolved URLs -------------------------------


class _StubProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None

    def poll(self) -> int | None:
        return None


def test_start_resolves_urls_onto_the_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CLI must be able to print the URL block without a second kubectl
    # call or a scheme heuristic of its own.
    class _Kubectl(_StubKubectl):
        def port_forward(self, **_kwargs: object) -> _StubProc:
            return _StubProc(pid=1234)

    svc = _service(_Kubectl(["*.kind.local"]), tmp_path)
    monkeypatch.setattr(
        "chart_manager.services.expose._local_port_open", lambda *_a, **_k: True
    )

    status = svc.start(ExposeRequest(cluster_name="c", service="ns/gw", ports=[]))

    assert status.apps_domain == "kind.local"
    assert [u.url for u in status.urls] == [
        "https://*.kind.local:8443/",
        "http://*.kind.local:8080/",
    ]
    assert [u.remote_port for u in status.urls] == ["443", "80"]
    assert status.pid == 1234


def test_status_read_from_disk_does_not_pay_for_url_resolution(tmp_path: Path) -> None:
    # `start` calls `status` to detect an existing forward; resolving URLs
    # there would mean a `kubectl get gateway -A` on every liveness probe.
    (tmp_path / "c.json").write_text(
        '{"cluster": "c", "service": "ns/gw", "ports": ["8443:443"], '
        f'"pid": {os.getpid()}, "log": "/tmp/c.log"}}'
    )

    status = _service(_StubKubectl(["*.kind.local"]), tmp_path).status("c")

    assert status is not None
    assert status.urls == ()
