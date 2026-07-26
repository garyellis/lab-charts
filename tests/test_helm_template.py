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


def _write_chart(path: Path, *, with_deps: bool) -> None:
    path.mkdir(parents=True, exist_ok=True)
    chart_yaml = "apiVersion: v2\nname: demo\nversion: 0.1.0\n"
    if with_deps:
        chart_yaml += (
            "dependencies:\n"
            "  - name: subchart\n"
            "    version: 1.0.0\n"
            "    repository: https://example.invalid/\n"
        )
    (path / "Chart.yaml").write_text(chart_yaml)


def test_template_emits_expected_args_without_deps(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    _write_chart(chart, with_deps=False)
    out_dir = tmp_path / "out"
    values = [tmp_path / "values.yaml"]
    values[0].write_text("key: value\n")

    runner = FakeCommandRunner()
    helm = Helm(runner=runner)

    result_path = helm.template(
        "demo-release",
        chart,
        namespace="demo-ns",
        output_dir=out_dir,
        values=values,
    )

    # Only one call (template) because there are no dependencies.
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


def test_template_runs_dependency_update_when_chart_has_deps(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    _write_chart(chart, with_deps=True)
    out_dir = tmp_path / "out"

    runner = FakeCommandRunner()
    helm = Helm(runner=runner)

    helm.template("r", chart, namespace="ns", output_dir=out_dir)

    assert len(runner.calls) == 2
    assert runner.calls[0][:3] == ("helm", "dependency", "update")
    assert runner.calls[0][3] == str(chart)
    assert runner.calls[1][1] == "template"


def test_template_skips_dependency_update_for_oci_ref(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    helm = Helm(runner=runner)

    helm.template("r", "oci://registry.example/charts/demo", namespace="ns", output_dir=tmp_path / "out")

    assert all(call[1] != "dependency" for call in runner.calls)
    assert any(call[1] == "template" for call in runner.calls)


def test_template_failure_reruns_with_debug_and_raises(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    _write_chart(chart, with_deps=False)
    out_dir = tmp_path / "out"

    runner = FakeCommandRunner().script(
        Reply(returncode=1, stderr="boom"), Reply(returncode=1, stderr="boom")
    )
    helm = Helm(runner=runner)

    with pytest.raises(ExternalCommandError) as exc:
        helm.template("r", chart, namespace="ns", output_dir=out_dir)

    assert len(runner.calls) == 2
    assert "--debug" in runner.calls[1]
    msg = str(exc.value)
    assert str(out_dir.resolve()) in msg
    assert "boom" in msg


def test_template_passes_api_versions_and_kube_version(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    _write_chart(chart, with_deps=False)

    runner = FakeCommandRunner()
    helm = Helm(runner=runner)

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


def test_template_skips_dependency_update_for_malformed_chart_yaml(tmp_path: Path) -> None:
    # Malformed YAML must not crash dep-detection; helm itself will surface
    # the real chart-loading error during template.
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("not: : valid: yaml:\n  - [\n")
    runner = FakeCommandRunner()
    helm = Helm(runner=runner)

    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out")

    # Only template runs — no dependency update was attempted.
    assert len(runner.calls) == 1
    assert runner.calls[0][1] == "template"


def test_template_skips_dependency_update_when_dependencies_is_non_list(tmp_path: Path) -> None:
    # A `dependencies:` field that isn't a list (e.g., string or null) must
    # not trigger `helm dependency update` — that command would error
    # cryptically rather than helm template surfacing the schema problem.
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: demo\nversion: 0.1.0\ndependencies: not-a-list\n"
    )
    runner = FakeCommandRunner()
    helm = Helm(runner=runner)

    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out")

    assert len(runner.calls) == 1
    assert runner.calls[0][1] == "template"


def test_template_with_skip_tests_false_omits_flag(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    _write_chart(chart, with_deps=False)

    runner = FakeCommandRunner()
    helm = Helm(runner=runner)

    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out", skip_tests=False)

    assert "--skip-tests" not in runner.calls[0]



def test_dependency_update_deduped_per_chart_path(tmp_path: Path) -> None:
    # Two template() calls on the same chart with deps should only run
    # `helm dependency update` once. This is the per-chart lock +
    # first-caller-wins behavior that makes validate run --all sane.
    chart = tmp_path / "chart"
    _write_chart(chart, with_deps=True)

    runner = FakeCommandRunner()
    helm = Helm(runner=runner)

    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out-a")
    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out-b")

    dep_calls = [c for c in runner.calls if c[1] == "dependency"]
    template_calls = [c for c in runner.calls if c[1] == "template"]
    assert len(dep_calls) == 1
    assert len(template_calls) == 2


def test_dependency_update_runs_per_distinct_chart(tmp_path: Path) -> None:
    chart_a = tmp_path / "a"
    chart_b = tmp_path / "b"
    _write_chart(chart_a, with_deps=True)
    _write_chart(chart_b, with_deps=True)

    runner = FakeCommandRunner()
    helm = Helm(runner=runner)

    helm.template("r", chart_a, namespace="ns", output_dir=tmp_path / "out-a")
    helm.template("r", chart_b, namespace="ns", output_dir=tmp_path / "out-b")

    dep_calls = [c for c in runner.calls if c[1] == "dependency"]
    assert len(dep_calls) == 2
    assert {c[3] for c in dep_calls} == {str(chart_a), str(chart_b)}


def test_verbose_false_passes_capture_true_to_dependency_update(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    _write_chart(chart, with_deps=True)

    runner = FakeCommandRunner()
    helm = Helm(runner=runner, verbose=False)

    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out")

    dep_calls = [r for r in runner.records if r.args[1] == "dependency"]
    assert dep_calls
    # capture=True == not streamed under concurrency.
    assert dep_calls[0].capture is True


def test_verbose_true_streams_dependency_update(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    _write_chart(chart, with_deps=True)

    runner = FakeCommandRunner()
    helm = Helm(runner=runner, verbose=True)

    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out")

    dep_calls = [r for r in runner.records if r.args[1] == "dependency"]
    assert dep_calls
    assert dep_calls[0].capture is False  # streamed, preserving legacy behavior


def test_template_honors_verbose_for_streaming(tmp_path: Path) -> None:
    # Regression: pre-fix, verbose=True only streamed dependency_update +
    # lint/upgrade/test, not the actual `helm template` subprocess.
    chart = tmp_path / "chart"
    _write_chart(chart, with_deps=False)

    runner = FakeCommandRunner()
    helm = Helm(runner=runner, verbose=True)
    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out")

    template_calls = [r for r in runner.records if r.args[1] == "template"]
    assert template_calls
    assert template_calls[0].capture is False  # capture=False -> stream


def test_template_captures_when_not_verbose(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    _write_chart(chart, with_deps=False)

    runner = FakeCommandRunner()
    helm = Helm(runner=runner, verbose=False)
    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out")

    template_calls = [r for r in runner.records if r.args[1] == "template"]
    assert template_calls
    assert template_calls[0].capture is True  # capture=True -> parallel-safe


def test_template_threads_timeout_to_runner(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    _write_chart(chart, with_deps=False)

    runner = FakeCommandRunner()
    helm = Helm(runner=runner, timeout=42.0)
    helm.template("r", chart, namespace="ns", output_dir=tmp_path / "out")

    template_call = next(r for r in runner.records if r.args[1] == "template")
    assert template_call.timeout == 42.0


def test_dependency_update_explicit_timeout_kwarg_wins(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    _write_chart(chart, with_deps=True)

    runner = FakeCommandRunner()
    # Instance-level timeout would be passed by .timeout; the explicit
    # `timeout=` kwarg on dependency_update is what the prefetch pass uses.
    helm = Helm(runner=runner, timeout=999.0)
    helm.dependency_update(chart, timeout=10.0)

    dep_call = next(r for r in runner.records if r.args[1] == "dependency")
    assert dep_call.timeout == 10.0
