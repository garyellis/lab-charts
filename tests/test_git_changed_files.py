"""Unit coverage for `Git.changed_files` (mirrors `changed_charts`)."""
from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.integrations.git import Git
from chart_manager.plumbing.errors import ExternalCommandError
from tests.conftest import FakeCommandRunner


def _runner(*, is_repo: bool, diff_stdout: str = "") -> FakeCommandRunner:
    """Answer the two commands `Git` issues: the repo probe and the listing."""
    return (
        FakeCommandRunner()
        # `is_repository` runs with check=False and reads the returncode;
        # 128 is what git returns outside a work tree.
        .respond(("git", "rev-parse"), returncode=0 if is_repo else 128)
        .respond(("git", "diff"), stdout=diff_stdout)
    )


def test_changed_files_returns_sorted_unique_paths(tmp_path: Path) -> None:
    runner = _runner(
        is_repo=True,
        diff_stdout="charts/a/values.yaml\ncharts/a/values.yaml\nREADME.md\n\n",
    )
    git = Git(tmp_path, runner=runner)

    assert git.changed_files(base="origin/main") == ["README.md", "charts/a/values.yaml"]
    # Confirm we issued `...HEAD` so feature-branch semantics match changed_charts.
    diff_call = next(c for c in runner.calls if c[:2] == ("git", "diff"))
    assert diff_call[-1] == "origin/main...HEAD"


def test_changed_files_raises_outside_git_repo(tmp_path: Path) -> None:
    runner = _runner(is_repo=False)
    git = Git(tmp_path, runner=runner)

    with pytest.raises(ExternalCommandError, match="not a git repository"):
        git.changed_files()


def test_changed_files_empty_diff_returns_empty_list(tmp_path: Path) -> None:
    runner = _runner(is_repo=True, diff_stdout="\n\n")
    git = Git(tmp_path, runner=runner)

    assert git.changed_files() == []
