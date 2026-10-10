"""`Helm.template` builds its command from the render options; output streams when verbose."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.errors import ExternalCommandError
from tests.conftest import FakeCommandRunner, Reply


def _helm(runner: FakeCommandRunner, *, verbose: bool = True) -> Helm:
    return Helm(runner, binary="helm", verbose=verbose, timeout=None, context=None)


@pytest.mark.parametrize(
    ("options", "flags"),
    [
        pytest.param({}, ("--skip-tests",), id="defaults"),
        pytest.param(
            {
                "values": [Path("values.yaml")],
                "sets": {"a": "1"},
                "api_versions": ["networking.k8s.io/v1"],
                "kube_version": "1.31.0",
                "skip_tests": False,
                "include_crds": True,
            },
            (
                "--values", "values.yaml", "--set", "a=1",
                "--api-versions", "networking.k8s.io/v1", "--kube-version", "1.31.0",
                "--include-crds",
            ),
            id="every-option",
        ),
    ],
)
def test_template_renders_into_the_output_dir_with_the_given_options(
    tmp_path: Path, options: dict[str, Any], flags: tuple[str, ...]
) -> None:
    runner = FakeCommandRunner()
    out = tmp_path / "out"

    rendered = _helm(runner).template("r", tmp_path, namespace="ns", output_dir=out, **options)

    assert rendered == out.resolve()
    # No --skip-schema-validation: subchart schema errors must fail the render.
    assert runner.calls == [
        ("helm", "template", "r", str(tmp_path), "--namespace", "ns",
         "--output-dir", str(out.resolve()), *flags)
    ]


def test_template_failure_raises_the_debug_rerun_detail_and_the_output_path(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner().script(
        Reply(returncode=1, stderr="boom"), Reply(returncode=1, stderr="boom: detail")
    )
    out = tmp_path / "out"

    with pytest.raises(ExternalCommandError) as exc:
        _helm(runner).template("r", tmp_path, namespace="ns", output_dir=out)

    assert str(out.resolve()) in str(exc.value)
    assert "boom: detail" in str(exc.value)


@pytest.mark.parametrize("verbose", [True, False])
def test_output_streams_only_when_verbose_and_answers_are_always_captured(
    tmp_path: Path, verbose: bool
) -> None:
    runner = FakeCommandRunner(stdout="[]")
    helm = _helm(runner, verbose=verbose)

    helm.lint(tmp_path, [])
    helm.template("r", tmp_path, namespace="ns", output_dir=tmp_path / "out")
    helm.upgrade_install("r", tmp_path, namespace="ns", timeout=60.0)
    helm.test("r", namespace="ns", timeout=60.0)
    helm.dependency_update(tmp_path, timeout=1.0)

    streamed = {r.args[1] for r in runner.records if not r.capture}
    assert streamed == ({"lint", "template", "upgrade", "test", "dependency"} if verbose else set())
