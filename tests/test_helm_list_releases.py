"""Coverage for `Helm.list_releases`.

The lab installer's skip-if-already-installed loop is driven by this; if
helm's JSON contract drifts we want a unit test to flag it rather than a
mysterious "always reinstalling" symptom in `local up`.
"""
from __future__ import annotations

import json

import pytest

from chart_manager.integrations.helm import Helm, ReleaseInfo
from chart_manager.plumbing.errors import ExternalCommandError
from tests.conftest import FakeCommandRunner


def _helm(runner: FakeCommandRunner) -> Helm:
    return Helm(runner, binary="helm", timeout=None, context=None)


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

    releases = _helm(FakeCommandRunner(stdout=payload)).list_releases()

    assert releases == [
        ReleaseInfo(name="cilium", namespace="kube-system", revision=1, status="deployed"),
        ReleaseInfo(name="grafana", namespace="observability", revision=3, status="deployed"),
    ]


def test_list_releases_empty_stdout_returns_empty_list() -> None:
    assert _helm(FakeCommandRunner(stdout="")).list_releases() == []


def test_list_releases_invalid_json_raises_external_command_error() -> None:
    with pytest.raises(ExternalCommandError):
        _helm(FakeCommandRunner(stdout="not-json")).list_releases()


def test_list_releases_tolerates_missing_revision() -> None:
    # Defensive: helm's contract has been stable, but a missing/garbled
    # revision should not blow up the install loop -- it just means we
    # surface 0 and continue.
    payload = json.dumps([{"name": "x", "namespace": "y", "status": "deployed"}])

    releases = _helm(FakeCommandRunner(stdout=payload)).list_releases()

    assert releases == [ReleaseInfo(name="x", namespace="y", revision=0, status="deployed")]
