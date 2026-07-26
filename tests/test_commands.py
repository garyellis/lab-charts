"""CommandRunner tests.

We don't unit-test the happy path here (covered by every integration test
shelling through a stub); we focus on the timeout path that's hard to
exercise via stubs and easy to regress.
"""
from __future__ import annotations

import os
import sys

import pytest

from chart_manager.plumbing.commands import SubprocessRunner, redact
from chart_manager.plumbing.errors import (
    CommandTimeout,
    ExternalCommandError,
    MissingToolError,
)


def test_timeout_raises_external_command_error() -> None:
    runner = SubprocessRunner()
    # `python -c "import time; time.sleep(...)"` is portable across platforms
    # and lets us assert the timeout fires without depending on /bin/sleep.
    with pytest.raises(ExternalCommandError) as exc:
        runner.run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=0.1,
        )
    msg = str(exc.value)
    assert "timed out" in msg
    assert "0.1" in msg


def test_unbounded_timeout_passes_through() -> None:
    # timeout=None must not trip subprocess.TimeoutExpired; quick echo run.
    runner = SubprocessRunner()
    result = runner.run([sys.executable, "-c", "print('ok')"])
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_timeout_zero_does_not_mean_disabled() -> None:
    # 0 is a valid subprocess timeout value (effectively immediate); the
    # CLI translates --row-timeout 0 to None at the CLI boundary, but at
    # the runner layer 0 must mean what subprocess.run says it means.
    runner = SubprocessRunner()
    with pytest.raises(ExternalCommandError):
        runner.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.0)


def test_timeout_is_typed_so_callers_need_not_match_on_wording() -> None:
    runner = SubprocessRunner()
    with pytest.raises(CommandTimeout):
        runner.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.1)


def test_failure_populates_stderr_and_returncode() -> None:
    """The structured fields exist on the error and must not be left empty.

    Consumers read `exc.stderr or str(exc)`; when the runner left stderr
    blank they silently fell through to the message on every failure, and
    only test fakes (which did populate it) exercised the real branch.
    """
    runner = SubprocessRunner()
    with pytest.raises(ExternalCommandError) as exc:
        runner.run(
            [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"]
        )

    assert exc.value.returncode == 3
    assert "boom" in exc.value.stderr


def test_a_missing_binary_is_a_typed_external_command_error() -> None:
    runner = SubprocessRunner()
    with pytest.raises(MissingToolError) as exc:
        runner.run(["definitely-not-a-real-binary-xyz"])

    # Subclasses ExternalCommandError so best-effort handlers still degrade.
    assert isinstance(exc.value, ExternalCommandError)
    assert "definitely-not-a-real-binary-xyz" in str(exc.value)


def test_env_is_visible_to_the_child() -> None:
    runner = SubprocessRunner()
    result = runner.run(
        [sys.executable, "-c", "import os; print(os.environ['CM_PROBE'])"],
        env={"CM_PROBE": "scoped"},
    )
    assert result.stdout.strip() == "scoped"


def test_env_does_not_leak_into_the_parent_process() -> None:
    """Scoping a variable to one invocation must not mutate os.environ.

    The whole point of `env=` is that a request can address a different
    cluster/daemon without the process it runs in changing underneath the
    requests running concurrently beside it.
    """
    runner = SubprocessRunner()
    runner.run([sys.executable, "-c", "pass"], env={"CM_PROBE": "scoped"})
    assert "CM_PROBE" not in os.environ


def test_env_overlays_rather_than_replaces_the_parent_environment() -> None:
    """A bare `env=` would drop PATH and every tool would look uninstalled.

    Asserting on PATH specifically because that is the variable whose loss
    turns a scoped override into a MissingToolError that reads like a
    broken install rather than like a caller mistake.
    """
    runner = SubprocessRunner()
    result = runner.run(
        [sys.executable, "-c", "import os; print(os.environ.get('PATH', ''))"],
        env={"CM_PROBE": "scoped"},
    )
    assert result.stdout.strip() == os.environ.get("PATH", "")


def test_failure_messages_do_not_leak_credential_values() -> None:
    runner = SubprocessRunner()
    with pytest.raises(ExternalCommandError) as exc:
        runner.run(
            [sys.executable, "-c", "import sys; sys.exit(1)", "--set", "admin.password=hunter2"]
        )

    msg = str(exc.value)
    assert "hunter2" not in msg
    assert "admin.password=***" in msg


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["helm", "--set", "a.b=secret"], "helm --set a.b=***"),
        (["helm", "--set-string", "k=v"], "helm --set-string k=***"),
        (["gh", "--token=abc123"], "gh --token=***"),
        (["helm", "--set", "bare"], "helm --set ***"),
        (["helm", "template", "."], "helm template ."),
    ],
)
def test_redact_masks_only_the_value_half(argv: list[str], expected: str) -> None:
    assert redact(argv) == expected
