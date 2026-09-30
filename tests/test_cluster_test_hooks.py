"""Host-side execution of one compiled cluster-test hook action."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.plumbing.commands import SubprocessRunner
from chart_manager.plumbing.errors import (
    ChartManagerError,
    CommandTimeout,
    ExternalCommandError,
)
from chart_manager.services.lifecycle.hooks import ClusterTestHookRunner
from chart_manager.services.lifecycle.models import ActionKind, ActionTarget, LifecycleAction

from .conftest import FakeCommandRunner


def _script(root: Path, body: str) -> str:
    path = root / "scripts" / "hook"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return "./scripts/hook"


def _action(
    root: Path,
    command: tuple[str, ...],
    *,
    kind: ActionKind = ActionKind.HOOK_PRE_INSTALL,
    timeout: str = "1m",
) -> LifecycleAction:
    return LifecycleAction(
        action_id=f"cluster-test.app.minimal.{kind.value}",
        kind=kind,
        target=ActionTarget(chart="app", profile="minimal", release="app", namespace="apps"),
        input_digest="digest",
        chart_path=root / "charts" / "app",
        timeout=timeout,
        command=command,
    )


def _runner(root: Path, runner: object | None = None) -> ClusterTestHookRunner:
    return ClusterTestHookRunner(
        root,
        runner=runner or SubprocessRunner(),  # type: ignore[arg-type]
        kube_context="kind-lab",
        cluster_name="lab",
    )


def test_hook_runs_argv_from_repo_root_with_run_coordinates_in_its_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PARENT_MARKER", "inherited")
    dump = tmp_path / "env-dump"
    argv = (_script(tmp_path, f'pwd > {dump}\necho "argv=$1" >> {dump}\nenv >> {dump}\n'), "$HOME")

    _runner(tmp_path).run(_action(tmp_path, argv, kind=ActionKind.HOOK_POST_INSTALL))

    lines = dump.read_text(encoding="utf-8").splitlines()
    assert Path(lines[0]).resolve() == tmp_path.resolve()
    assert lines[1] == "argv=$HOME"
    env = dict(line.split("=", 1) for line in lines[2:] if "=" in line)
    assert env["PARENT_MARKER"] == "inherited"
    assert {key: value for key, value in env.items() if key.startswith("CHART_MANAGER_")} == {
        "CHART_MANAGER_HOOK_PHASE": "post-install",
        "CHART_MANAGER_ROOT": str(tmp_path.resolve()),
        "CHART_MANAGER_CHART": "app",
        "CHART_MANAGER_CHART_PATH": str(tmp_path / "charts" / "app"),
        "CHART_MANAGER_PROFILE": "minimal",
        "CHART_MANAGER_RELEASE": "app",
        "CHART_MANAGER_NAMESPACE": "apps",
        "CHART_MANAGER_KUBE_CONTEXT": "kind-lab",
        "CHART_MANAGER_CLUSTER_NAME": "lab",
    }


def test_hook_timeout_is_the_profile_timeout_and_output_is_captured(tmp_path: Path) -> None:
    runner = FakeCommandRunner()

    _runner(tmp_path, runner).run(_action(tmp_path, ("tool", "arg"), timeout="7m"))

    (record,) = runner.records
    assert record.args == ("tool", "arg")
    assert record.timeout == 420.0
    assert record.capture is True
    assert record.cwd == tmp_path.resolve()


def test_hook_with_a_non_finite_timeout_is_refused_before_it_runs(tmp_path: Path) -> None:
    # float("nan") used to become the subprocess timeout, which never fires:
    # the hook could hang forever. It is now an invalid duration.
    runner = FakeCommandRunner()
    with pytest.raises(ChartManagerError, match="invalid duration: 'nan'"):
        _runner(tmp_path, runner).run(_action(tmp_path, ("tool",), timeout="nan"))
    assert runner.records == []


def test_failed_hook_reports_exit_code_and_verbatim_stderr_tail(tmp_path: Path) -> None:
    argv = (
        _script(
            tmp_path,
            'i=1\nwhile [ $i -le 25 ]; do echo "line $i" >&2; i=$((i + 1)); done\n'
            "echo 'retry with --token s3cret' >&2\n"
            "exit 3\n",
        ),
        "--password",
        "hunter2",
    )

    with pytest.raises(ExternalCommandError) as raised:
        _runner(tmp_path).run(_action(tmp_path, argv))

    message = str(raised.value)
    assert raised.value.returncode == 3
    assert message.startswith(
        "pre-install hook exited 3: ./scripts/hook --password ***\n"
    )
    assert "line 6\n" not in message
    assert "line 7\n" in message
    # The configured argv is masked; the script's own stderr is verbatim.
    assert message.endswith("line 25\nretry with --token s3cret")
    assert "hunter2" not in message


def test_hook_exceeding_the_profile_timeout_reports_the_timeout_and_stderr(
    tmp_path: Path,
) -> None:
    argv = (_script(tmp_path, "echo 'waiting on --token s3cret' >&2\nexec sleep 5\n"),)

    with pytest.raises(CommandTimeout) as raised:
        _runner(tmp_path).run(
            _action(tmp_path, argv, kind=ActionKind.HOOK_CLEANUP, timeout="0.5s")
        )

    assert str(raised.value) == (
        "cleanup hook timed out after 0.5s: ./scripts/hook\nwaiting on --token s3cret"
    )


def test_hook_logs_that_it_runs_and_its_output_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    argv = (_script(tmp_path, 'echo "hello $2"\necho "warn" >&2\n'), "--token", "s3cret")
    caplog.set_level("DEBUG", logger="chart_manager.services.lifecycle.hooks")

    _runner(tmp_path).run(_action(tmp_path, argv))

    info = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    debug = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "DEBUG")
    assert info == ["running pre-install hook for app/minimal: ./scripts/hook --token ***"]
    # The configured argv is masked; the script's own output is verbatim.
    assert "hello s3cret" in debug
    assert "warn" in debug
