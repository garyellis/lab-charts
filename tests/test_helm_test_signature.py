"""Helm cluster addressing, and `Helm.test`.

`Helm.test` returns the CommandResult whatever the exit code, so callers can
judge the verdict without catching ExternalCommandError.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.integrations.helm import Helm
from tests.conftest import FakeCommandRunner


def _helm(runner: FakeCommandRunner) -> Helm:
    return Helm(runner, binary="helm", timeout=None, context=None)


def test_every_cluster_call_is_pinned_to_the_context_and_timeout(tmp_path: Path) -> None:
    runner = FakeCommandRunner(stdout="[]")
    helm = Helm(runner, binary="helm", timeout=30.0, context="kind-a")

    helm.lint(tmp_path, [])
    helm.template("r", tmp_path, namespace="ns", output_dir=tmp_path / "out")
    helm.upgrade_install("r", tmp_path, namespace="ns", timeout=60.0)
    helm.manifest("r", namespace="ns")
    helm.test("r", namespace="ns", timeout=60.0, subprocess_timeout=None)
    helm.test("r", namespace="ns", timeout=60.0, subprocess_timeout=2.0)
    helm.dependency_update(tmp_path, timeout=2.0)

    assert {r.args[-2:] for r in runner.records} == {("--kube-context", "kind-a")}
    # A per-call budget overrides the instance cap.
    assert {r.timeout for r in runner.records[:-2]} == {30.0}
    assert {r.timeout for r in runner.records[-2:]} == {2.0}


def test_test_returns_a_failed_result_without_raising() -> None:
    runner = FakeCommandRunner(returncode=1, stderr="Error: test failed")

    result = _helm(runner).test("loki", namespace="loki", timeout=60.0, subprocess_timeout=None)

    assert (result.returncode, result.stderr) == (1, "Error: test failed")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (300.0, "300s"),
        (300, "300s"),
        (1.5, "1.5s"),
        (45.25, "45.25s"),
        (0.1, "0.1s"),
        (1e-05, "0.00001s"),
        (0.0, "0s"),
    ],
)
def test_test_renders_its_wait_as_plain_go_seconds(seconds: float, expected: str) -> None:
    # Go's time.ParseDuration rejects the "1e-05s" exponent form.
    runner = FakeCommandRunner()

    _helm(runner).test(
        "loki", namespace="loki", timeout=seconds, logs=True, subprocess_timeout=None
    )

    assert runner.calls == [
        ("helm", "test", "loki", "--namespace", "loki", "--timeout", expected, "--logs")
    ]


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), -1.0])
def test_test_rejects_a_wait_that_is_not_a_duration(seconds: float) -> None:
    with pytest.raises(ValueError):
        _helm(FakeCommandRunner()).test(
            "loki", namespace="loki", timeout=seconds, subprocess_timeout=None
        )
