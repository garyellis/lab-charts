from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from chart_manager.integrations.github import Github
from chart_manager.plumbing.commands import CommandResult, CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError


class _Runner(CommandRunner):
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._stdout = stdout
        self._returncode = returncode

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(args))
        if check and self._returncode != 0:
            raise ExternalCommandError(f"command failed ({self._returncode})")
        return CommandResult(
            args=tuple(args),
            returncode=self._returncode,
            stdout=self._stdout,
            stderr="",
        )


def test_create_pr_invokes_gh_with_expected_flags(tmp_path: Path) -> None:
    runner = _Runner(stdout="https://github.com/o/r/pull/7\n")
    gh = Github(tmp_path, runner=runner)

    pr = gh.create_pr(
        title="chore(prod): promote loki to 0.1.2",
        body="body",
        head="promote/prod/loki-0.1.2",
        base="main",
    )

    assert pr.url == "https://github.com/o/r/pull/7"
    assert runner.calls[0] == (
        "gh",
        "pr",
        "create",
        "--title",
        "chore(prod): promote loki to 0.1.2",
        "--body",
        "body",
        "--head",
        "promote/prod/loki-0.1.2",
        "--base",
        "main",
    )


def test_create_pr_draft_passes_flag(tmp_path: Path) -> None:
    runner = _Runner(stdout="https://x/1")
    Github(tmp_path, runner=runner).create_pr(
        title="t", body="b", head="h", base="main", draft=True
    )
    assert "--draft" in runner.calls[0]


def test_find_open_pr_returns_none_when_empty(tmp_path: Path) -> None:
    runner = _Runner(stdout="[]")
    assert Github(tmp_path, runner=runner).find_open_pr_for_branch("foo") is None


def test_find_open_pr_parses_first_match(tmp_path: Path) -> None:
    payload = json.dumps([{"url": "https://x/9", "number": 9}])
    runner = _Runner(stdout=payload)
    pr = Github(tmp_path, runner=runner).find_open_pr_for_branch("foo")
    assert pr is not None
    assert pr.url == "https://x/9"
    assert pr.number == 9


def test_find_open_pr_raises_on_non_json(tmp_path: Path) -> None:
    runner = _Runner(stdout="not json")
    with pytest.raises(ExternalCommandError, match="non-JSON"):
        Github(tmp_path, runner=runner).find_open_pr_for_branch("foo")
