"""`Git.preflight`: the binary and whether the root is a checkout."""

from __future__ import annotations

from pathlib import Path

from chart_manager.integrations.git import Git
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import CheckStatus
from tests.conftest import FakeCommandRunner, OnPath, checks_by_name


def test_a_root_that_is_not_a_checkout_is_reported(on_path: OnPath, tmp_path: Path) -> None:
    """Indistinguishable from "nothing changed" at every CI selector otherwise."""
    on_path("git")
    runner = FakeCommandRunner()
    runner.respond(("git", "--version"), stdout="git version 2.47.0\n")
    runner.respond(("git", "rev-parse"), returncode=128, stderr="not a git repository\n")

    repository = checks_by_name(Git(tmp_path, runner, timeout=None).preflight())["git-repository"]

    assert repository.status is CheckStatus.FAILED
    assert repository.outcome is Outcome.ENVIRONMENT
