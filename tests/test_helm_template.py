from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.integrations import helm as helm_module
from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.errors import ExternalCommandError
from tests.conftest import FakeCommandRunner, Reply


@pytest.fixture(autouse=True)
def _clear_mise_cache() -> None:
    helm_module._clear_mise_cache()


def test_template_emits_expected_args(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    out_dir = tmp_path / "out"
    values = [tmp_path / "values.yaml"]
    values[0].write_text("key: value\n")

    runner = FakeCommandRunner()
    helm = Helm(runner)

    result_path = helm.template(
        "demo-release",
        chart,
        namespace="demo-ns",
        output_dir=out_dir,
        values=values,
    )

    # Only one call: template does not fetch dependencies.
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call[0] == "helm"
    assert call[1] == "template"
    assert call[2] == "demo-release"
    assert call[3] == str(chart)
    assert "--namespace" in call and call[call.index("--namespace") + 1] == "demo-ns"
    assert "--output-dir" in call and call[call.index("--output-dir") + 1] == str(out_dir.resolve())
    assert "--values" in call and call[call.index("--values") + 1] == str(values[0])
    assert "--skip-tests" in call
    assert "--skip-schema-validation" not in call
    assert result_path == out_dir.resolve()


def test_template_failure_reruns_with_debug_and_raises(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    out_dir = tmp_path / "out"

    runner = FakeCommandRunner().script(
        Reply(returncode=1, stderr="boom"), Reply(returncode=1, stderr="boom")
    )
    helm = Helm(runner)

    with pytest.raises(ExternalCommandError) as exc:
        helm.template("r", chart, namespace="ns", output_dir=out_dir)

    assert len(runner.calls) == 2
    assert "--debug" in runner.calls[1]
    msg = str(exc.value)
    assert str(out_dir.resolve()) in msg
    assert "boom" in msg


def test_template_passes_api_versions_and_kube_version(tmp_path: Path) -> None:
    chart = tmp_path / "chart"

    runner = FakeCommandRunner()
    helm = Helm(runner)

    helm.template(
        "r",
        chart,
        namespace="ns",
        output_dir=tmp_path / "out",
        api_versions=["networking.k8s.io/v1"],
        kube_version="1.31.0",
    )

    call = runner.calls[0]
    assert "--api-versions" in call and call[call.index("--api-versions") + 1] == "networking.k8s.io/v1"
    assert "--kube-version" in call and call[call.index("--kube-version") + 1] == "1.31.0"


def test_template_with_skip_tests_false_omits_flag(tmp_path: Path) -> None:
    chart = tmp_path / "chart"

    runner = FakeCommandRunner()
    helm = Helm(runner)

    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out", skip_tests=False)

    assert "--skip-tests" not in runner.calls[0]


def test_template_can_include_crds(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    runner = FakeCommandRunner()
    helm = Helm(runner)

    helm.template(
        "r",
        chart,
        namespace="ns",
        output_dir=tmp_path / "out",
        include_crds=True,
    )

    assert "--include-crds" in runner.calls[0]


def test_template_honors_verbose_for_streaming(tmp_path: Path) -> None:
    # Regression: pre-fix, verbose=True only streamed dependency_update +
    # lint/upgrade/test, not the actual `helm template` subprocess.
    chart = tmp_path / "chart"

    runner = FakeCommandRunner()
    helm = Helm(runner, verbose=True)
    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out")

    template_calls = [r for r in runner.records if r.args[1] == "template"]
    assert template_calls
    assert template_calls[0].capture is False  # capture=False -> stream


def test_template_captures_when_not_verbose(tmp_path: Path) -> None:
    chart = tmp_path / "chart"

    runner = FakeCommandRunner()
    helm = Helm(runner, verbose=False)
    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out")

    template_calls = [r for r in runner.records if r.args[1] == "template"]
    assert template_calls
    assert template_calls[0].capture is True  # capture=True -> parallel-safe


def test_template_threads_timeout_to_runner(tmp_path: Path) -> None:
    chart = tmp_path / "chart"

    runner = FakeCommandRunner()
    helm = Helm(runner, timeout=42.0)
    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out")

    template_call = next(r for r in runner.records if r.args[1] == "template")
    assert template_call.timeout == 42.0


@pytest.mark.parametrize("verbose", [True, False])
def test_dependency_update_carries_the_timeout_and_streams_only_when_verbose(
    tmp_path: Path, verbose: bool
) -> None:
    runner = FakeCommandRunner()

    Helm(runner, verbose=verbose, timeout=42.0).dependency_update(tmp_path)

    [call] = runner.records
    assert call.args == ("helm", "dependency", "update", str(tmp_path))
    assert (call.timeout, call.capture) == (42.0, not verbose)
