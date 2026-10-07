from __future__ import annotations

from pathlib import Path

from chart_manager.integrations.helm import Helm
from tests.conftest import FakeCommandRunner


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


def test_a_pinned_version_resolves_through_mise_where() -> None:
    runner = FakeCommandRunner(stdout="/opt/helm/3.20.0\n")

    instance = Helm(runner=runner, version="3.20.0")

    assert instance._helm_bin == "/opt/helm/3.20.0/bin/helm"
    assert runner.calls == [("mise", "where", "helm@3.20.0")]


def test_resolved_binary_is_used_in_commands() -> None:
    runner = FakeCommandRunner()

    instance = Helm(runner=runner, binary="/custom/helm")
    instance.dependency_update(Path("charts/grafana"), timeout=1.0)

    assert runner.calls == [("/custom/helm", "dependency", "update", "charts/grafana")]
