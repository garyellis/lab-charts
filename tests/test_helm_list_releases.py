"""Coverage for `Helm.list_releases`.

The lab installer's skip-if-already-installed loop is driven by this; if
helm's JSON contract drifts we want a unit test to flag it rather than a
mysterious "always reinstalling" symptom in `sandbox up`.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from chart_manager.integrations import helm as helm_module
from chart_manager.integrations.helm import Helm, ReleaseInfo
from chart_manager.plumbing.commands import CommandResult, CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError


class FakeRunner(CommandRunner):
    def __init__(self, stdout: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self._stdout = stdout

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(args))
        return CommandResult(args=tuple(args), returncode=0, stdout=self._stdout, stderr="")


@pytest.fixture(autouse=True)
def _clear_mise_cache() -> None:
    helm_module._resolve_via_mise.cache_clear()


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
    runner = FakeRunner(stdout=payload)

    instance = Helm(runner=runner)
    releases = instance.list_releases()

    assert runner.calls == [("helm", "list", "-o", "json", "-A")]
    assert releases == [
        ReleaseInfo(name="cilium", namespace="kube-system", revision=1, status="deployed"),
        ReleaseInfo(name="grafana", namespace="observability", revision=3, status="deployed"),
    ]


def test_list_releases_empty_stdout_returns_empty_list() -> None:
    runner = FakeRunner(stdout="")

    releases = Helm(runner=runner).list_releases()

    assert releases == []


def test_list_releases_namespace_scoped_drops_all_flag() -> None:
    runner = FakeRunner(stdout="[]")

    Helm(runner=runner).list_releases(all_namespaces=False, namespace="observability")

    assert runner.calls == [("helm", "list", "-o", "json", "-n", "observability")]


def test_list_releases_invalid_json_raises_external_command_error() -> None:
    runner = FakeRunner(stdout="not-json")

    with pytest.raises(ExternalCommandError):
        Helm(runner=runner).list_releases()


def test_list_releases_tolerates_missing_revision() -> None:
    # Defensive: helm's contract has been stable, but a missing/garbled
    # revision should not blow up the install loop -- it just means we
    # surface 0 and continue.
    payload = json.dumps([{"name": "x", "namespace": "y", "status": "deployed"}])
    runner = FakeRunner(stdout=payload)

    releases = Helm(runner=runner).list_releases()

    assert releases == [ReleaseInfo(name="x", namespace="y", revision=0, status="deployed")]
