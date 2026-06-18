"""Coverage for `Helm.test`'s extended signature.

The helmrelease test service relies on a `check=False` Helm.test that
returns a CommandResult so it can classify rc != 0 outcomes without
catching ExternalCommandError. Existing ci/sandbox callers still pass
only `(release, namespace=, timeout=)` and discard the return.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from chart_manager.integrations import helm as helm_module
from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.commands import CommandResult, CommandRunner


class FakeRunner(CommandRunner):
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.calls.append(
            (
                tuple(args),
                {"cwd": cwd, "check": check, "capture": capture, "timeout": timeout},
            )
        )
        return CommandResult(
            args=tuple(args),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


@pytest.fixture(autouse=True)
def _clear_mise_cache() -> None:
    helm_module._resolve_via_mise.cache_clear()


def test_test_legacy_kwargs_still_work_and_return_command_result() -> None:
    runner = FakeRunner(returncode=0, stdout="PASS")
    result = Helm(runner=runner).test("loki", namespace="loki", timeout="5m")

    assert isinstance(result, CommandResult)
    assert result.returncode == 0
    assert result.stdout == "PASS"
    args, kwargs = runner.calls[0]
    assert args == ("helm", "test", "loki", "--namespace", "loki", "--timeout", "5m")
    assert kwargs["check"] is False


def test_test_logs_flag_appends_logs_argv() -> None:
    runner = FakeRunner()
    Helm(runner=runner).test("loki", namespace="loki", logs=True)

    args, _ = runner.calls[0]
    assert "--logs" in args
    # ordering: --logs comes after --timeout
    assert args.index("--logs") > args.index("--timeout")


def test_test_subprocess_timeout_plumbs_to_runner() -> None:
    runner = FakeRunner()
    Helm(runner=runner).test(
        "loki", namespace="loki", subprocess_timeout=42.5
    )

    _, kwargs = runner.calls[0]
    assert kwargs["timeout"] == 42.5


def test_test_subprocess_timeout_defaults_to_instance_timeout() -> None:
    runner = FakeRunner()
    Helm(runner=runner, timeout=99.0).test("loki", namespace="loki")

    _, kwargs = runner.calls[0]
    assert kwargs["timeout"] == 99.0


def test_test_check_false_returns_failed_command_result_without_raising() -> None:
    runner = FakeRunner(returncode=1, stderr="Error: test failed")
    result = Helm(runner=runner).test("loki", namespace="loki")

    assert result.returncode == 1
    assert "test failed" in result.stderr


def test_context_kwarg_appends_kube_context_flag() -> None:
    runner = FakeRunner()
    Helm(runner=runner, context="kind-foo").test("loki", namespace="loki")
    args, _ = runner.calls[0]
    assert args[-2:] == ("--kube-context", "kind-foo")


def test_context_default_omits_kube_context_flag() -> None:
    runner = FakeRunner()
    Helm(runner=runner).test("loki", namespace="loki")
    args, _ = runner.calls[0]
    assert "--kube-context" not in args
