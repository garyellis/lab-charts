import json
from pathlib import Path
from typing import Any, cast

import pytest

from chart_manager.commands.upgrade import UpgradeError, UpgradeRequest, UpgradeResult
from chart_manager.commands.upgrade.run import build_upgrade_plan, run
from chart_manager.commands.upgrade.wire import upgrade_to_dict
from chart_manager.plumbing.errors import ExternalCommandError
from tests.conftest import CHARTS_DIR, FakeCommandRunner, Reply, workspace_for


def _chart(tmp_path: Path) -> Path:
    chart = tmp_path / "charts" / "my-chart"
    chart.mkdir(parents=True, exist_ok=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: my-chart\nversion: 0.4.2\n", encoding="utf-8"
    )
    return chart


def test_plan_has_deterministic_branch_group_and_scoped_overlay(tmp_path: Path) -> None:
    chart = _chart(tmp_path)
    first = build_upgrade_plan(tmp_path, chart, charts_dir=CHARTS_DIR)
    second = build_upgrade_plan(tmp_path, Path("my-chart"), charts_dir=CHARTS_DIR)
    assert first.branch_prefix == second.branch_prefix == "renovate/my-chart/"
    assert first.group == second.group == "chart-manager:my-chart"
    assert first.runtime_overlay["packageRules"][0]["matchFileNames"] == ["charts/my-chart/**"]
    assert first.runtime_overlay["enabledManagers"] == ["helmv3", "helm-values", "custom.regex"]
    assert first.runtime_overlay["lockFileMaintenance"] == {"enabled": False}
    # The chart scope and its matching branch namespace must outrank the
    # repository's own renovate.json, which Renovate merges last.
    assert first.runtime_overlay["force"] == {
        "includePaths": ["charts/my-chart/**"],
        "branchPrefix": "renovate/my-chart/",
        "branchPrefixOld": "renovate/my-chart/",
    }
    callback = first.runtime_overlay["postUpgradeTasks"]
    assert callback["commands"] == [
        "chart-manager upgrade-finalize --path charts/my-chart"
    ]
    assert '"updateType":"{{updateType}}"' in callback["dataFileTemplate"]


@pytest.mark.parametrize("version", ["01.2.3", "1.02.3", "1.2.03", "1.2.3-rc.1"])
def test_plan_rejects_a_wrapper_version_that_is_not_strict_x_y_z(
    tmp_path: Path, version: str
) -> None:
    chart = _chart(tmp_path)
    (chart / "Chart.yaml").write_text(f"name: my-chart\nversion: {version}\n", encoding="utf-8")
    with pytest.raises(UpgradeError, match=r"strict x\.y\.z"):
        build_upgrade_plan(tmp_path, chart, charts_dir=CHARTS_DIR)


class _RecordingEvents:
    """EventWriter stand-in that records build events."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.events: list[dict[str, object]] = []
        self._raises = raises

    def build(self, **kwargs: object) -> None:
        self.events.append(kwargs)
        if self._raises is not None:
            raise self._raises


_BRANCH = "renovate/my-chart/my-chart"


def _prs(*branches: str) -> Reply:
    """`gh pr list` output with one open pull request per branch, numbered 7, 9, ..."""
    return Reply(
        stdout=json.dumps(
            [
                {
                    "url": f"https://example.test/pull/{7 + 2 * index}",
                    "number": 7 + 2 * index,
                    "baseRefName": "main",
                    "headRefName": branch,
                }
                for index, branch in enumerate(branches)
            ]
        )
    )


def _chart_at(version: str) -> Reply:
    """`gh api` output: the chart's Chart.yaml on the upgrade branch."""
    return Reply(stdout=f"apiVersion: v2\nname: my-chart\nversion: {version}\n")


def _runner(
    *,
    renovate: Reply | None = None,
    prs: tuple[Reply, ...] = (_prs(),),
    branch_files: tuple[Reply, ...] = (Reply(),),
) -> FakeCommandRunner:
    return (
        FakeCommandRunner()
        .respond(("git", "remote"), stdout="git@github.com:owner/repository.git\n")
        .respond_each(("renovate",), renovate or Reply())
        .respond_each(("gh", "pr", "list"), *prs)
        .respond_each(("gh", "api"), *branch_files)
    )


def _upgrade(
    tmp_path: Path,
    runner: FakeCommandRunner,
    *,
    dry_run: bool = False,
    events: _RecordingEvents | None = None,
) -> UpgradeResult:
    chart = _chart(tmp_path)
    (tmp_path / "renovate-global.json").write_text("{}\n", encoding="utf-8")
    return run(
        UpgradeRequest(root=tmp_path, chart_path=chart, dry_run=dry_run),
        workspace=workspace_for(tmp_path),
        runner=runner,
        events=cast(Any, events if events is not None else _RecordingEvents()),
    )


def _branch_reads(runner: FakeCommandRunner) -> list[tuple[str, ...]]:
    return [call for call in runner.calls if call[:2] == ("gh", "api")]


def test_dry_run_drives_git_and_renovate_through_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ci-token")
    (tmp_path / "charts" / "my-chart").mkdir(parents=True)
    (tmp_path / "charts" / "my-chart" / "renovate.json").write_text("{}\n", encoding="utf-8")
    runner = _runner(renovate=Reply(stdout="renovate complete\n"))

    result = _upgrade(tmp_path, runner, dry_run=True)

    assert result.outcome == "dry_run"
    assert result.current_version == "0.4.2"
    assert upgrade_to_dict(result)["path"] == (tmp_path / "charts/my-chart").resolve().as_posix()
    assert runner.calls[0] == ("git", "remote", "get-url", "origin")
    assert runner.calls[1] == (
        "git", "status", "--porcelain=v1", "--untracked-files=all", "--",
        "charts/my-chart", "renovate-global.json", "renovate.json",
    )
    assert runner.calls[2:] == [("renovate", "owner/repository")]
    env = runner.records[2].env
    assert env is not None
    assert env["RENOVATE_DRY_RUN"] == "full"
    # RENOVATE_TOKEN first, then the token GitHub Actions provides.
    assert env["RENOVATE_TOKEN"] == "ci-token"
    assert env["RENOVATE_ADDITIONAL_CONFIG_FILE"] == str(
        (tmp_path / "charts/my-chart/renovate.json").resolve()
    )


def test_projects_new_and_existing_pull_request_status(tmp_path: Path) -> None:
    runner = _runner(prs=(_prs(), _prs(_BRANCH)), branch_files=(_chart_at("0.4.3"),))

    opened = _upgrade(tmp_path, runner)

    assert opened.outcome == "pr_open"
    assert opened.pr_url == "https://example.test/pull/7"
    assert opened.pr_number == 7
    assert opened.repository == "owner/repository"
    # Reported from the PR's head ref, not re-derived from Renovate's naming.
    assert opened.branch == _BRANCH


def test_repository_comes_from_github_repository_before_the_origin_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "ci/charts")
    runner = _runner()

    result = _upgrade(tmp_path, runner, dry_run=True)

    assert result.repository == "ci/charts"
    assert ("git", "remote", "get-url", "origin") not in runner.calls


def test_proposed_version_is_read_back_from_the_upgrade_branch(tmp_path: Path) -> None:
    runner = _runner(prs=(_prs(_BRANCH),), branch_files=(_chart_at("0.4.3"),))

    result = _upgrade(tmp_path, runner)

    assert result.proposed_version == "0.4.3"
    assert result.changed is True
    # Read twice against the same branch file: once before Renovate to capture
    # the proposal already on the branch (so a no-op re-run can be told from a
    # retarget), once after for the reported version.
    assert _branch_reads(runner) == [
        (
            "gh", "api",
            f"repos/{{owner}}/{{repo}}/contents/charts/my-chart/Chart.yaml?ref={_BRANCH}",
            "-H", "Accept: application/vnd.github.raw",
        )
    ] * 2
    assert result.diagnostics == ()


def test_unbumped_wrapper_version_on_the_branch_is_reported(tmp_path: Path) -> None:
    # Renovate records a failed or disallowed post-upgrade command as an
    # artifact error, then opens the pull request and exits zero, so an
    # unbumped wrapper version is the only signal this process can see.
    result = _upgrade(
        tmp_path, _runner(prs=(_prs(_BRANCH),), branch_files=(_chart_at("0.4.2"),))
    )

    assert result.proposed_version == "0.4.2"
    assert any("may not have run" in line for line in result.diagnostics)


def test_unreadable_branch_file_degrades_to_a_diagnostic(tmp_path: Path) -> None:
    unavailable = Reply(raises=ExternalCommandError("gh api failed"))

    result = _upgrade(tmp_path, _runner(prs=(_prs(_BRANCH),), branch_files=(unavailable,)))

    assert result.proposed_version is None
    # A failed read is a reporting gap, not an unknown pull-request status.
    assert result.outcome == "pr_updated"
    assert any("proposed wrapper version unavailable" in line for line in result.diagnostics)


def test_no_branch_read_without_a_pull_request(tmp_path: Path) -> None:
    runner = _runner()

    result = _upgrade(tmp_path, runner)

    assert result.outcome == "no_changes"
    assert result.proposed_version is None
    assert _branch_reads(runner) == []
    assert result.diagnostics == (
        "Renovate completed without an open pull request under "
        "renovate/my-chart/; no eligible update was proposed",
    )


def test_rejects_renovate_errors_logged_with_zero_exit(tmp_path: Path) -> None:
    renovate = Reply(
        stdout='ERROR: Repository has unknown error\n  "errorMessage": "Bad credentials"'
    )

    with pytest.raises(UpgradeError, match="Bad credentials"):
        _upgrade(tmp_path, _runner(renovate=renovate))


def test_preserves_renovate_warning_headlines(tmp_path: Path) -> None:
    renovate = Reply(stdout="WARN: Package lookup failed\nINFO: Repository finished")

    result = _upgrade(tmp_path, _runner(renovate=renovate))

    assert result.diagnostics[0] == "WARN: Package lookup failed"


def test_reports_drift_when_a_chart_holds_more_than_one_branch(tmp_path: Path) -> None:
    both = _prs("renovate/my-chart/a", "renovate/my-chart/b")

    result = _upgrade(tmp_path, _runner(prs=(both,), branch_files=(_chart_at("0.4.3"),)))

    assert result.outcome == "pr_updated"
    assert any("multiple open pull requests" in line for line in result.diagnostics)


def test_does_not_report_no_changes_when_pr_status_is_unavailable(tmp_path: Path) -> None:
    unavailable = Reply(raises=ExternalCommandError("gh unavailable"))

    result = _upgrade(tmp_path, _runner(prs=(unavailable,)))

    assert result.outcome == "status_unknown"
    assert result.diagnostics == (
        "pull-request status unavailable: gh unavailable",
        "pull-request status unavailable: gh unavailable",
    )


def test_rejects_relevant_uncommitted_inputs_before_renovate(tmp_path: Path) -> None:
    runner = _runner().respond(("git", "status"), stdout=" M charts/my-chart/values.yaml\n")

    with pytest.raises(UpgradeError, match="uncommitted changes"):
        _upgrade(tmp_path, runner)

    assert not any(call[0] == "renovate" for call in runner.calls)


# ----- build-lifecycle telemetry ------------------------------------------
#
# The mapping itself is covered in test_telemetry.py. What matters here is
# that run() emits from the *fully projected* result and only after the
# upgrade is already pushed -- so these tests assert the payload's provenance,
# and that emission cannot break the run.


def test_emits_the_version_it_read_back_from_the_branch(tmp_path: Path) -> None:
    events = _RecordingEvents()
    # No pull request beforehand, one after: the run that opens it.
    runner = _runner(prs=(_prs(), _prs(_BRANCH)), branch_files=(_chart_at("0.4.3"),))

    result = _upgrade(tmp_path, runner, events=events)

    assert result.proposed_version == "0.4.3"
    assert len(events.events) == 1
    emitted = events.events[0]
    assert emitted["chart_name"] == "my-chart"
    assert emitted["chart_version"] == "0.4.3"
    assert emitted["build_correlation_id"] == "owner/repository#7"


def test_emits_nothing_when_the_version_read_failed(tmp_path: Path) -> None:
    """An open PR whose version could not be read must not write "my-chart@None"."""
    events = _RecordingEvents()
    unavailable = Reply(raises=ExternalCommandError("gh api failed"))

    result = _upgrade(
        tmp_path, _runner(prs=(_prs(_BRANCH),), branch_files=(unavailable,)), events=events
    )

    assert result.outcome == "pr_updated"
    assert events.events == []


def test_dry_run_emits_nothing(tmp_path: Path) -> None:
    """Nothing was pushed, so there is no artifact to report."""
    events = _RecordingEvents()

    _upgrade(tmp_path, _runner(), dry_run=True, events=events)

    assert events.events == []


def test_a_failed_emission_does_not_fail_the_upgrade(tmp_path: Path) -> None:
    """The branch is already pushed by the time telemetry runs."""
    events = _RecordingEvents(raises=RuntimeError("cosmos unreachable"))
    runner = _runner(prs=(_prs(), _prs(_BRANCH)), branch_files=(_chart_at("0.4.3"),))

    result = _upgrade(tmp_path, runner, events=events)

    assert result.proposed_version == "0.4.3"


def test_rerun_against_an_unchanged_pull_request_emits_nothing(tmp_path: Path) -> None:
    """run() must read the branch *before* Renovate, or it cannot tell."""
    events = _RecordingEvents()
    runner = _runner(prs=(_prs(_BRANCH),), branch_files=(_chart_at("0.4.3"),))

    result = _upgrade(tmp_path, runner, events=events)

    assert result.outcome == "pr_updated"
    assert result.proposed_version == "0.4.3"
    # Read twice: once before Renovate for the baseline proposal, once after.
    assert len(_branch_reads(runner)) == 2
    assert events.events == []
    # The pre-read must not leak diagnostics about work the operator did not ask for.
    assert result.diagnostics == ()


def test_rerun_that_retargets_the_version_still_emits(tmp_path: Path) -> None:
    """A major update superseding a patch moves the target while the PR stays open."""
    events = _RecordingEvents()
    runner = _runner(
        prs=(_prs(_BRANCH),), branch_files=(_chart_at("0.4.3"), _chart_at("1.0.0"))
    )

    result = _upgrade(tmp_path, runner, events=events)

    assert result.outcome == "pr_updated"
    assert result.proposed_version == "1.0.0"
    assert len(events.events) == 1
    assert events.events[0]["chart_version"] == "1.0.0"


def test_no_pre_read_when_no_pull_request_is_open(tmp_path: Path) -> None:
    """The opening run has no branch to compare against, so it cannot be skipped."""
    events = _RecordingEvents()
    runner = _runner(prs=(_prs(), _prs(_BRANCH)), branch_files=(_chart_at("0.4.3"),))

    result = _upgrade(tmp_path, runner, events=events)

    assert result.outcome == "pr_open"
    assert len(_branch_reads(runner)) == 1  # only the post-Renovate read
    assert len(events.events) == 1
