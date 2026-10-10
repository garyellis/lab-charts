"""`Github` PR lookups and creation, answered from gh's output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_manager.integrations.github import Github, PullRequest
from chart_manager.plumbing.errors import ExternalCommandError
from tests.conftest import FakeCommandRunner


def _github(runner: FakeCommandRunner, root: Path) -> Github:
    return Github(root, runner, timeout=None)


def test_every_call_runs_in_the_repo_within_the_timeout(tmp_path: Path) -> None:
    runner = FakeCommandRunner(stdout="[]")
    github = Github(tmp_path, runner, timeout=30.0)

    github.find_open_pr_for_branch("b", base="main")
    github.find_open_prs_for_branch_prefix("renovate/")
    github.read_file_at_ref("Chart.yaml", "main")
    github.create_pr(title="t", body="b", head="h", base="main")

    assert {(r.cwd, r.timeout) for r in runner.records} == {(tmp_path, 30.0)}


def test_create_pr_answers_the_last_url_gh_prints(tmp_path: Path) -> None:
    runner = FakeCommandRunner(stdout="Warning: 1 uncommitted change\nhttps://x/pull/7\n")

    pr = _github(runner, tmp_path).create_pr(title="t", body="b", head="h", base="main")

    assert pr == PullRequest(url="https://x/pull/7", number=None)


@pytest.mark.parametrize(
    ("listed", "expected"),
    [
        pytest.param([], None, id="none-open"),
        pytest.param(
            [
                {"url": "https://x/8", "number": 8},
                {"url": "https://x/9", "number": 9},
            ],
            PullRequest(url="https://x/8", number=8),
            id="first-listed",
        ),
    ],
)
def test_find_open_pr_answers_the_first_listed_pr(
    tmp_path: Path, listed: list[dict[str, object]], expected: PullRequest | None
) -> None:
    runner = FakeCommandRunner(stdout=json.dumps(listed))

    assert _github(runner, tmp_path).find_open_pr_for_branch("b", base="main") == expected


def test_a_pr_listing_that_is_not_json_raises(tmp_path: Path) -> None:
    github = _github(FakeCommandRunner(stdout="not json"), tmp_path)

    with pytest.raises(ExternalCommandError, match="non-JSON"):
        github.find_open_pr_for_branch("b", base="main")
    with pytest.raises(ExternalCommandError, match="non-JSON"):
        github.find_open_prs_for_branch_prefix("renovate/")


def test_prefix_lookup_keeps_only_the_matching_namespace(tmp_path: Path) -> None:
    payload = json.dumps(
        [
            {"url": "https://x/9", "number": 9, "headRefName": "renovate/loki/major-loki"},
            {"url": "https://x/4", "number": 4, "headRefName": "renovate/loki/loki"},
            {"url": "https://x/5", "number": 5, "headRefName": "renovate/loki-gateway/x"},
            {"url": "https://x/6", "number": 6, "headRefName": "feature/manual"},
        ]
    )
    runner = FakeCommandRunner(stdout=payload)
    found = _github(runner, tmp_path).find_open_prs_for_branch_prefix("renovate/loki/")

    # A sibling chart whose name merely starts with the same characters must
    # not be captured; the trailing slash is what makes the namespace exact.
    assert [pr.branch for pr in found] == ["renovate/loki/loki", "renovate/loki/major-loki"]
