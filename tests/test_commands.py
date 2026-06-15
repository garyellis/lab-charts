"""CommandRunner tests.

We don't unit-test the happy path here (covered by every integration test
shelling through a stub); we focus on the timeout path that's hard to
exercise via stubs and easy to regress.
"""
from __future__ import annotations

import sys

import pytest

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError


def test_timeout_raises_external_command_error() -> None:
    runner = CommandRunner()
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
    runner = CommandRunner()
    result = runner.run([sys.executable, "-c", "print('ok')"])
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_timeout_zero_does_not_mean_disabled() -> None:
    # 0 is a valid subprocess timeout value (effectively immediate); the
    # CLI translates --row-timeout 0 to None at the CLI boundary, but at
    # the runner layer 0 must mean what subprocess.run says it means.
    runner = CommandRunner()
    with pytest.raises(ExternalCommandError):
        runner.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.0)
