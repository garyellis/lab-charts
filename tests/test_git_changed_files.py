"""Unit coverage for `Git.changed_files` (mirrors `changed_charts`)."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from chart_manager.integrations.git import Git
from chart_manager.plumbing.commands import CommandResult, CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError


class _Runner(CommandRunner):
    def __init__(self, *, is_repo: bool, diff_stdout: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self._is_repo = is_repo
        self._diff_stdout = diff_stdout

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        argv = tuple(args)
        self.calls.append(argv)
        if argv[:2] == ("git", "rev-parse"):
            rc = 0 if self._is_repo else 128
            return CommandResult(args=argv, returncode=rc, stdout="", stderr="")
        if argv[:2] == ("git", "diff"):
            return CommandResult(args=argv, returncode=0, stdout=self._diff_stdout, stderr="")
        return CommandResult(args=argv, returncode=0, stdout="", stderr="")


def test_changed_files_returns_sorted_unique_paths(tmp_path: Path) -> None:
    runner = _Runner(
        is_repo=True,
        diff_stdout="charts/a/values.yaml\ncharts/a/values.yaml\nREADME.md\n\n",
    )
    git = Git(tmp_path, runner=runner)

    assert git.changed_files(base="origin/main") == ["README.md", "charts/a/values.yaml"]
    # Confirm we issued `...HEAD` so feature-branch semantics match changed_charts.
    diff_call = next(c for c in runner.calls if c[:2] == ("git", "diff"))
    assert diff_call[-1] == "origin/main...HEAD"


def test_changed_files_raises_outside_git_repo(tmp_path: Path) -> None:
    runner = _Runner(is_repo=False)
    git = Git(tmp_path, runner=runner)

    with pytest.raises(ExternalCommandError, match="not a git repository"):
        git.changed_files()


def test_changed_files_empty_diff_returns_empty_list(tmp_path: Path) -> None:
    runner = _Runner(is_repo=True, diff_stdout="\n\n")
    git = Git(tmp_path, runner=runner)

    assert git.changed_files() == []
