"""`Github.preflight`: the gh binary and its authentication."""

from __future__ import annotations

from pathlib import Path

from chart_manager.integrations.github import Github
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import CheckStatus
from tests.conftest import FakeCommandRunner, OnPath, checks_by_name

#: A real `gh auth status` report: a bare host header, then marked per-account
#: lines. Verbatim because the shape is the whole reason `_status_line` exists.
_GH_AUTH_FAILURE = (
    "github.com\n"
    "  X Failed to log in to github.com using token (GITHUB_TOKEN)\n"
    "  - The token in GITHUB_TOKEN is invalid.\n"
)


def test_unauthenticated_gh_is_an_environment_failure(on_path: OnPath, tmp_path: Path) -> None:
    """`gh` installed predicts nothing about whether a promote can open a PR."""
    on_path("gh")
    runner = FakeCommandRunner()
    runner.respond(("gh", "--version"), stdout="gh version 2.62.0\n")
    runner.respond(("gh", "auth", "status"), returncode=1, stderr=_GH_AUTH_FAILURE)

    auth = checks_by_name(Github(tmp_path, runner, timeout=None).preflight())["gh-auth"]

    assert auth.status is CheckStatus.FAILED
    assert auth.outcome is Outcome.ENVIRONMENT
    assert "Failed to log in" in auth.detail, "the host header alone is not a diagnostic"
