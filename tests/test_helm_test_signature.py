"""Coverage for `Helm.test`'s extended signature.

The helmrelease test service relies on a `check=False` Helm.test that
returns a CommandResult so it can classify rc != 0 outcomes without
catching ExternalCommandError. Existing ci/sandbox callers still pass
only `(release, namespace=, timeout=)` and discard the return.
"""
from __future__ import annotations

import pytest

from chart_manager.integrations import helm as helm_module
from chart_manager.integrations.helm import Helm, format_helm_duration
from chart_manager.plumbing.commands import CommandResult
from tests.conftest import FakeCommandRunner


@pytest.fixture(autouse=True)
def _clear_mise_cache() -> None:
    helm_module._clear_mise_cache()


def test_test_legacy_kwargs_still_work_and_return_command_result() -> None:
    runner = FakeCommandRunner(returncode=0, stdout="PASS")
    result = Helm(runner=runner).test("loki", namespace="loki", timeout="5m")

    assert isinstance(result, CommandResult)
    assert result.returncode == 0
    assert result.stdout == "PASS"
    call = runner.records[0]
    assert call.args == ("helm", "test", "loki", "--namespace", "loki", "--timeout", "5m")
    assert call.check is False


def test_test_logs_flag_appends_logs_argv() -> None:
    runner = FakeCommandRunner()
    Helm(runner=runner).test("loki", namespace="loki", logs=True)

    call = runner.records[0]
    assert "--logs" in call.args
    # ordering: --logs comes after --timeout
    assert call.args.index("--logs") > call.args.index("--timeout")


def test_test_subprocess_timeout_plumbs_to_runner() -> None:
    runner = FakeCommandRunner()
    Helm(runner=runner).test(
        "loki", namespace="loki", subprocess_timeout=42.5
    )

    call = runner.records[0]
    assert call.timeout == 42.5


def test_test_subprocess_timeout_defaults_to_instance_timeout() -> None:
    runner = FakeCommandRunner()
    Helm(runner=runner, timeout=99.0).test("loki", namespace="loki")

    call = runner.records[0]
    assert call.timeout == 99.0


def test_test_check_false_returns_failed_command_result_without_raising() -> None:
    runner = FakeCommandRunner(returncode=1, stderr="Error: test failed")
    result = Helm(runner=runner).test("loki", namespace="loki")

    assert result.returncode == 1
    assert "test failed" in result.stderr


def test_context_kwarg_appends_kube_context_flag() -> None:
    runner = FakeCommandRunner()
    Helm(runner=runner, context="kind-foo").test("loki", namespace="loki")
    call = runner.records[0]
    assert call.args[-2:] == ("--kube-context", "kind-foo")


def test_context_default_omits_kube_context_flag() -> None:
    runner = FakeCommandRunner()
    Helm(runner=runner).test("loki", namespace="loki")
    call = runner.records[0]
    assert "--kube-context" not in call.args


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (300.0, "300s"),
        (300, "300s"),
        (1.5, "1.5s"),
        (45.25, "45.25s"),
        (0.1, "0.1s"),
        (1e-05, "0.00001s"),
        (3600.0, "3600s"),
        (0.0, "0s"),
    ],
)
def test_format_helm_duration_is_plain_go_seconds(seconds: float, expected: str) -> None:
    # No exponent form and no trailing ".0": both would be either rejected by
    # Go's time.ParseDuration or needlessly noisy on the helm command line.
    assert format_helm_duration(seconds) == expected


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), -1.0])
def test_format_helm_duration_rejects_invalid_seconds(seconds: float) -> None:
    with pytest.raises(ValueError):
        format_helm_duration(seconds)
