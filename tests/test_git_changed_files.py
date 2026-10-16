"""Unit coverage for `Git` queries."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from chart_manager.integrations.git import Git
from chart_manager.plumbing.commands import SubprocessRunner
from chart_manager.plumbing.errors import ExternalCommandError
from tests.conftest import FakeCommandRunner, workspace_for


def _runner(*, is_repo: bool, diff_stdout: str = "") -> FakeCommandRunner:
    """Answer the two commands `Git` issues: the repo probe and the listing."""
    return (
        FakeCommandRunner()
        # `is_repository` runs with check=False and reads the returncode;
        # 128 is what git returns outside a work tree.
        .respond(("git", "rev-parse"), returncode=0 if is_repo else 128)
        .respond(("git", "diff"), stdout=diff_stdout)
    )


def _local(runner: FakeCommandRunner, root: Path) -> Git:
    return Git(root, runner, timeout=None)


def test_every_call_runs_in_the_root_within_the_timeout(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    root = tmp_path / "clone"
    git = Git.clone("url", root, branch="main", runner=runner, timeout=30.0)

    git.is_repository()
    git.checkout_new_branch("b", base="main")
    git.add(["a"])
    git.commit("m", body="b")
    git.push("b")
    git.status_paths([Path("charts")])
    git.remote_url()
    git.show("HEAD", Path("a"))
    git.changed_files()

    assert root.is_dir()
    assert {(r.cwd, r.timeout) for r in runner.records} == {(root, 30.0)}


def test_changed_files_returns_sorted_unique_paths(tmp_path: Path) -> None:
    runner = _runner(
        is_repo=True,
        diff_stdout="charts/a/values.yaml\ncharts/a/values.yaml\nREADME.md\n\n",
    )

    assert _local(runner, tmp_path).changed_files() == ["README.md", "charts/a/values.yaml"]


def test_changed_files_raises_outside_git_repo(tmp_path: Path) -> None:
    with pytest.raises(ExternalCommandError, match="not a git repository"):
        _local(_runner(is_repo=False), tmp_path).changed_files()


def test_remote_url_is_none_without_an_origin_remote(tmp_path: Path) -> None:
    assert _local(FakeCommandRunner(returncode=2), tmp_path).remote_url() is None


def test_show_raises_when_the_revision_lacks_the_file(tmp_path: Path) -> None:
    runner = FakeCommandRunner(returncode=128, stderr="fatal: path does not exist")

    with pytest.raises(ExternalCommandError) as raised:
        _local(runner, tmp_path).show("HEAD", Path("charts/demo/Chart.yaml"))
    assert raised.value.stderr == "fatal: path does not exist"


def _git(cwd: Path, *args: str) -> str:
    """Run real git hermetically: no user config, signing or hooks apply."""
    result = subprocess.run(
        [
            "git",
            "-c", "user.name=test",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_changed_files_are_relative_to_a_workspace_below_the_git_top_level(
    tmp_path: Path,
) -> None:
    """A repository root nested in a larger checkout sees its own paths only.

    Without `--relative`, git reports `platform/charts/demo/values.yaml`, which
    no chart prefix or fanout pattern under `platform/` can match, so change
    detection silently selected nothing.
    """
    workspace_root = tmp_path / "platform"
    chart = workspace_root / "charts" / "demo"
    chart.mkdir(parents=True)
    (chart / "values.yaml").write_text("a: 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("top\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (chart / "values.yaml").write_text("a: 2\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("changed outside the workspace\n", encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "change")

    changed = Git(workspace_root, SubprocessRunner(), timeout=None).changed_files(base=base)

    assert changed == ["charts/demo/values.yaml"]
    workspace = workspace_for(workspace_root)
    assert [workspace.chart_name_from_repo_path(path) for path in changed] == ["demo"]
