"""Apps-domain detection used by `sandbox expose` for the URL print.

The CLI handler queries `kubectl get gateway -A`, strips `*.` from each
host, and picks the most-common suffix as the apps-domain. Fallback to
`localhost` when no Gateway is installed yet (pre-lab or sandbox-test
path). The behaviour must be deterministic on ties so the printed URL
is reproducible across runs.
"""
from __future__ import annotations

from chart_manager.cli.main import _APPS_DOMAIN_FALLBACK, _detect_apps_domain


class _StubKubectl:
    def __init__(self, hosts: list[str]) -> None:
        self._hosts = hosts

    def list_gateway_hosts(self) -> list[str]:
        return list(self._hosts)


def test_detect_apps_domain_returns_fallback_when_no_gateway() -> None:
    assert _detect_apps_domain(_StubKubectl([])) == _APPS_DOMAIN_FALLBACK


def test_detect_apps_domain_strips_wildcard_prefix() -> None:
    assert _detect_apps_domain(_StubKubectl(["*.localhost"])) == "localhost"


def test_detect_apps_domain_picks_most_common_suffix() -> None:
    hosts = [
        "*.localhost",
        "*.localhost",
        "*.k8s.home.lab.io",
    ]
    assert _detect_apps_domain(_StubKubectl(hosts)) == "localhost"


def test_detect_apps_domain_breaks_ties_alphabetically() -> None:
    # Two suffixes each appear once -- alphabetical pick: kind.local <
    # localhost, so kind.local wins regardless of input order.
    hosts = ["*.localhost", "*.kind.local"]
    assert _detect_apps_domain(_StubKubectl(hosts)) == "kind.local"


def test_detect_apps_domain_handles_bare_host_without_wildcard() -> None:
    # A non-wildcard host (e.g. a one-off explicit host) is used as-is.
    hosts = ["foo.kind.local", "*.kind.local", "*.kind.local"]
    assert _detect_apps_domain(_StubKubectl(hosts)) == "kind.local"
