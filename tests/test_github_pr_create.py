from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_manager.integrations.github import Github
from chart_manager.plumbing.errors import ExternalCommandError
from tests.conftest import FakeCommandRunner


def test_create_pr_invokes_gh_with_expected_flags(tmp_path: Path) -> None:
    runner = FakeCommandRunner(stdout="https://github.com/o/r/pull/7\n")
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
    runner = FakeCommandRunner(stdout="https://x/1")
    Github(tmp_path, runner=runner).create_pr(
        title="t", body="b", head="h", base="main", draft=True
    )
    assert "--draft" in runner.calls[0]


def test_find_open_pr_returns_none_when_empty(tmp_path: Path) -> None:
    runner = FakeCommandRunner(stdout="[]")
    assert Github(tmp_path, runner=runner).find_open_pr_for_branch("foo") is None


def test_find_open_pr_parses_first_match(tmp_path: Path) -> None:
    payload = json.dumps([{"url": "https://x/9", "number": 9}])
    runner = FakeCommandRunner(stdout=payload)
    pr = Github(tmp_path, runner=runner).find_open_pr_for_branch("foo")
    assert pr is not None
    assert pr.url == "https://x/9"
    assert pr.number == 9


def test_find_open_pr_raises_on_non_json(tmp_path: Path) -> None:
    runner = FakeCommandRunner(stdout="not json")
    with pytest.raises(ExternalCommandError, match="non-JSON"):
        Github(tmp_path, runner=runner).find_open_pr_for_branch("foo")


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
    found = Github(tmp_path, runner=runner).find_open_prs_for_branch_prefix("renovate/loki/")

    # A sibling chart whose name merely starts with the same characters must
    # not be captured; the trailing slash is what makes the namespace exact.
    assert [pr.branch for pr in found] == ["renovate/loki/loki", "renovate/loki/major-loki"]
    assert "--head" not in runner.calls[0]
    assert "headRefName" in runner.calls[0][runner.calls[0].index("--json") + 1]
