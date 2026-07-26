from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.integrations import helm as helm_module
from chart_manager.integrations.helm import Helm
from tests.conftest import FakeCommandRunner


@pytest.fixture(autouse=True)
def _clear_mise_cache() -> None:
    helm_module._clear_mise_cache()


def test_resolve_defaults_to_path_helm() -> None:
    runner = FakeCommandRunner()

    instance = Helm(runner=runner)

    assert instance._helm_bin == "helm"
    assert runner.calls == []


def test_resolve_uses_explicit_binary_without_mise() -> None:
    runner = FakeCommandRunner()

    instance = Helm(runner=runner, binary="/opt/helm/4.1.3/bin/helm")

    assert instance._helm_bin == "/opt/helm/4.1.3/bin/helm"
    assert runner.calls == []


def test_resolve_binary_precedes_version() -> None:
    runner = FakeCommandRunner(stdout="/should/not/be/used")

    instance = Helm(runner=runner, version="3.20.0", binary="/explicit/helm")

    assert instance._helm_bin == "/explicit/helm"
    assert runner.calls == []


def test_resolve_via_mise_shells_command_runner() -> None:
    runner = FakeCommandRunner(stdout="/opt/helm/3.20.0\n")

    instance = Helm(runner=runner, version="3.20.0")

    assert instance._helm_bin == "/opt/helm/3.20.0/bin/helm"
    assert runner.calls == [("mise", "where", "helm@3.20.0")]


def test_resolve_via_mise_caches_by_version() -> None:
    # The memo is keyed by runner identity, then version. Reusing the same
    # runner across two Helm() constructions exercises the hit path; a
    # separate runner would (correctly) miss and reshell.
    runner = FakeCommandRunner(stdout="/opt/helm/3.20.0\n")

    Helm(runner=runner, version="3.20.0")
    instance = Helm(runner=runner, version="3.20.0")

    assert instance._helm_bin == "/opt/helm/3.20.0/bin/helm"
    assert runner.calls == [("mise", "where", "helm@3.20.0")]


def test_resolved_binary_is_used_in_commands() -> None:
    runner = FakeCommandRunner()

    instance = Helm(runner=runner, binary="/custom/helm")
    instance.dependency_update(Path("charts/grafana"))

    assert runner.calls == [("/custom/helm", "dependency", "update", "charts/grafana")]
