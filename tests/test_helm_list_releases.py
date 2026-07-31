"""Coverage for `Helm.list_releases`.

The lab installer's skip-if-already-installed loop is driven by this; if
helm's JSON contract drifts we want a unit test to flag it rather than a
mysterious "always reinstalling" symptom in `local up`.
"""
from __future__ import annotations

import json

import pytest

from chart_manager.integrations import helm as helm_module
from chart_manager.integrations.helm import Helm, ReleaseInfo
from chart_manager.plumbing.errors import ExternalCommandError
from tests.conftest import FakeCommandRunner


@pytest.fixture(autouse=True)
def _clear_mise_cache() -> None:
    helm_module._clear_mise_cache()


def test_list_releases_all_namespaces_parses_json() -> None:
    payload = json.dumps(
        [
            {
                "name": "cilium",
                "namespace": "kube-system",
                "revision": "1",
                "status": "deployed",
                "chart": "cilium-1.0.0",
            },
            {
                "name": "grafana",
                "namespace": "observability",
                "revision": "3",
                "status": "deployed",
            },
        ]
    )
    runner = FakeCommandRunner(stdout=payload)

    instance = Helm(runner=runner)
    releases = instance.list_releases()

    assert runner.calls == [("helm", "list", "-o", "json", "-A")]
    assert releases == [
        ReleaseInfo(name="cilium", namespace="kube-system", revision=1, status="deployed"),
        ReleaseInfo(name="grafana", namespace="observability", revision=3, status="deployed"),
    ]


def test_list_releases_empty_stdout_returns_empty_list() -> None:
    runner = FakeCommandRunner(stdout="")

    releases = Helm(runner=runner).list_releases()

    assert releases == []


def test_list_releases_namespace_scoped_drops_all_flag() -> None:
    runner = FakeCommandRunner(stdout="[]")

    Helm(runner=runner).list_releases(all_namespaces=False, namespace="observability")

    assert runner.calls == [("helm", "list", "-o", "json", "-n", "observability")]


def test_list_releases_invalid_json_raises_external_command_error() -> None:
    runner = FakeCommandRunner(stdout="not-json")

    with pytest.raises(ExternalCommandError):
        Helm(runner=runner).list_releases()


def test_list_releases_tolerates_missing_revision() -> None:
    # Defensive: helm's contract has been stable, but a missing/garbled
    # revision should not blow up the install loop -- it just means we
    # surface 0 and continue.
    payload = json.dumps([{"name": "x", "namespace": "y", "status": "deployed"}])
    runner = FakeCommandRunner(stdout=payload)

    releases = Helm(runner=runner).list_releases()

    assert releases == [ReleaseInfo(name="x", namespace="y", revision=0, status="deployed")]
