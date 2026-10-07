"""`chart upgrade` through `run`: git, Renovate and gh faked at the command runner."""

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from chart_manager.commands.upgrade import (
    UpgradeError,
    UpgradeRequest,
    UpgradeResult,
    UpgradeStatus,
)
from chart_manager.commands.upgrade.run import run
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.shared.events.model import BuildPhase, PlatformLifecycleEvent
from chart_manager.shared.events.store import EventQuery
from chart_manager.shared.events.writer import EventWriter
from tests.conftest import FakeCommandRunner, Reply, workspace_for

_BRANCH = "renovate/my-chart/my-chart"


class _EventLog:
    """An event store that records each event, and raises `raises` after recording it."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.events: list[PlatformLifecycleEvent] = []
        self.raises = raises

    def write(self, event: PlatformLifecycleEvent) -> None:
        self.events.append(event)
        if self.raises is not None:
            raise self.raises

    def query(self, query: EventQuery) -> list[dict[str, object]]:
        raise NotImplementedError


def _prs(*branches: str, numbered: bool = True) -> Reply:
    """`gh pr list` output with one open pull request per branch, numbered 7, 9, ..."""
    return Reply(
        stdout=json.dumps(
            [
                {
                    "url": f"https://example.test/pull/{7 + 2 * index}",
                    "baseRefName": "main",
                    "headRefName": branch,
                    **({"number": 7 + 2 * index} if numbered else {}),
                }
                for index, branch in enumerate(branches)
            ]
        )
    )


def _chart_at(version: str) -> Reply:
    """`gh api` output: the chart's Chart.yaml on the upgrade branch."""
    return Reply(stdout=f"apiVersion: v2\nname: my-chart\nversion: {version}\n")


_UNAVAILABLE = Reply(raises=ExternalCommandError("gh unavailable"))


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
    events: _EventLog | None = None,
    version: str = "0.4.2",
) -> UpgradeResult:
    chart = tmp_path / "charts" / "my-chart"
    chart.mkdir(parents=True, exist_ok=True)
    (chart / "Chart.yaml").write_text(
        f"apiVersion: v2\nname: my-chart\nversion: {version}\n", encoding="utf-8"
    )
    (tmp_path / "renovate-global.json").write_text("{}\n", encoding="utf-8")
    return run(
        UpgradeRequest(chart_path=chart, dry_run=dry_run),
        workspace=workspace_for(tmp_path),
        runner=runner,
        events=EventWriter(source="chart-manager", store=lambda: events if events is not None else _EventLog()),
        renovate_token=SecretStr("renovate-token"),
    )


def _renovate_env(runner: FakeCommandRunner) -> dict[str, str]:
    (record,) = [record for record in runner.records if record.args[0] == "renovate"]
    assert record.env is not None
    return dict(record.env)


def _branch_reads(runner: FakeCommandRunner) -> list[tuple[str, ...]]:
    return [call for call in runner.calls if call[:2] == ("gh", "api")]


# ----- what Renovate is handed ---------------------------------------------


def test_dry_run_drives_git_and_renovate_through_the_runner(tmp_path: Path) -> None:
    (tmp_path / "charts" / "my-chart").mkdir(parents=True)
    (tmp_path / "charts" / "my-chart" / "renovate.json").write_text("{}\n", encoding="utf-8")
    runner = _runner(renovate=Reply(stdout="renovate complete\n"))

    result = _upgrade(tmp_path, runner, dry_run=True)

    assert result.outcome is UpgradeStatus.DRY_RUN
    assert result.current_version == "0.4.2"
    assert result.chart_path == (tmp_path / "charts/my-chart").resolve()
    assert runner.calls == [
        ("git", "remote", "get-url", "origin"),
        (
            "git", "status", "--porcelain=v1", "--untracked-files=all", "--",
            "charts/my-chart", "renovate-global.json", "renovate.json",
        ),
        ("renovate", "owner/repository"),
    ]  # fmt: skip
    env = _renovate_env(runner)
    assert env["RENOVATE_DRY_RUN"] == "full"
    assert env["RENOVATE_TOKEN"] == "renovate-token"
    assert env["RENOVATE_CONFIG_FILE"] == str((tmp_path / "renovate-global.json").resolve())
    assert env["RENOVATE_ADDITIONAL_CONFIG_FILE"] == str(
        (tmp_path / "charts/my-chart/renovate.json").resolve()
    )


def test_renovate_is_scoped_to_one_chart_and_its_own_branch_namespace(tmp_path: Path) -> None:
    runner = _runner()

    result = _upgrade(tmp_path, runner)

    assert result.group == "chart-manager:my-chart"
    overlay = json.loads(_renovate_env(runner)["RENOVATE_CONFIG"])
    rule = overlay["packageRules"][0]
    assert rule["matchFileNames"] == ["charts/my-chart/**"]
    assert rule["groupName"] == "chart-manager:my-chart"
    assert overlay["enabledManagers"] == ["helmv3", "helm-values", "custom.regex"]
    assert overlay["lockFileMaintenance"] == {"enabled": False}
    # The chart scope and its matching branch namespace must outrank the
    # repository's own renovate.json, which Renovate merges last.
    assert overlay["force"] == {
        "includePaths": ["charts/my-chart/**"],
        "branchPrefix": "renovate/my-chart/",
        "branchPrefixOld": "renovate/my-chart/",
    }
    callback = overlay["postUpgradeTasks"]
    assert callback["commands"] == ["chart-manager upgrade-finalize --path charts/my-chart"]
    assert '"updateType":"{{updateType}}"' in callback["dataFileTemplate"]


@pytest.mark.parametrize("version", ["01.2.3", "1.02.3", "1.2.03", "1.2.3-rc.1"])
def test_a_wrapper_version_that_is_not_strict_x_y_z_is_rejected_before_renovate(
    tmp_path: Path, version: str
) -> None:
    runner = _runner()

    with pytest.raises(UpgradeError, match=r"strict x\.y\.z"):
        _upgrade(tmp_path, runner, version=version)

    assert not any(call[0] == "renovate" for call in runner.calls)


def test_relevant_uncommitted_inputs_are_rejected_before_renovate(tmp_path: Path) -> None:
    runner = _runner().respond(("git", "status"), stdout=" M charts/my-chart/values.yaml\n")

    with pytest.raises(UpgradeError, match="uncommitted changes"):
        _upgrade(tmp_path, runner)

    assert not any(call[0] == "renovate" for call in runner.calls)


def test_repository_comes_from_github_repository_before_the_origin_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "ci/charts")
    runner = _runner()

    result = _upgrade(tmp_path, runner, dry_run=True)

    assert result.repository == "ci/charts"
    assert ("git", "remote", "get-url", "origin") not in runner.calls


# ----- Renovate's own report -----------------------------------------------


@pytest.mark.parametrize(
    ("renovate", "message"),
    [
        (Reply(returncode=1, stderr="renovate crashed"), "Renovate failed.*renovate crashed"),
        # Renovate can exit zero after a repository-scoped failure it logged to stdout.
        (
            Reply(stdout='ERROR: Repository has unknown error\n  "errorMessage": "Bad credentials"'),
            "Bad credentials",
        ),
    ],
)
def test_a_failed_renovate_run_is_an_upgrade_error(
    tmp_path: Path, renovate: Reply, message: str
) -> None:
    with pytest.raises(UpgradeError, match=message):
        _upgrade(tmp_path, _runner(renovate=renovate))


def test_renovate_stderr_and_warning_headlines_become_diagnostics(tmp_path: Path) -> None:
    renovate = Reply(
        stdout="WARN: Package lookup failed\nINFO: Repository finished",
        stderr="node deprecation notice\n",
    )

    result = _upgrade(tmp_path, _runner(renovate=renovate))

    assert result.diagnostics[:2] == ("node deprecation notice", "WARN: Package lookup failed")


# ----- outcomes, the proposed version and telemetry -------------------------


def test_opening_a_pull_request_records_pr_open_for_the_proposed_version(
    tmp_path: Path,
) -> None:
    events = _EventLog()
    # No pull request before Renovate, one after: the run that opens it.
    runner = _runner(prs=(_prs(), _prs(_BRANCH)), branch_files=(_chart_at("0.4.3"),))

    result = _upgrade(tmp_path, runner, events=events)

    assert result.outcome is UpgradeStatus.PR_OPEN
    assert result.proposed_version == "0.4.3"
    assert result.pr_url == "https://example.test/pull/7"
    assert result.pr_number == 7
    assert result.repository == "owner/repository"
    # Reported from the PR's head ref, not re-derived from Renovate's naming.
    assert result.branch == _BRANCH
    assert result.diagnostics == ()
    # The opening run has no branch to compare against: only the post-Renovate read.
    assert _branch_reads(runner) == [
        (
            "gh", "api",
            f"repos/{{owner}}/{{repo}}/contents/charts/my-chart/Chart.yaml?ref={_BRANCH}",
            "-H", "Accept: application/vnd.github.raw",
        )
    ]  # fmt: skip
    (event,) = events.events
    assert event.build_phase is BuildPhase.PR_OPEN
    assert event.chart_name == "my-chart"
    # The proposed version, not the baseline: correlation_id names the artifact the PR proposes.
    assert event.chart_version == "0.4.3"
    assert event.correlation_id == "my-chart@0.4.3"
    # CI reconstructs this from github.repository and github.event.number.
    assert event.build_correlation_id == "owner/repository#7"
    assert event.pr_url == "https://example.test/pull/7"
    # Only str values: the DynamoDB adapter hands detail to boto3, which rejects float.
    assert event.detail == {
        "outcome": "pr_open",
        "previous_version": "0.4.2",
        "group": "chart-manager:my-chart",
        "branch": _BRANCH,
    }


def test_rerun_against_an_unchanged_pull_request_records_nothing(tmp_path: Path) -> None:
    """The branch is read before Renovate, so a re-run that proposes nothing new is silent."""
    events = _EventLog()
    runner = _runner(prs=(_prs(_BRANCH),), branch_files=(_chart_at("0.4.3"),))

    result = _upgrade(tmp_path, runner, events=events)

    assert result.outcome is UpgradeStatus.PR_UPDATED
    assert result.proposed_version == "0.4.3"
    # Read twice against the same branch file: once before Renovate, once after.
    assert len(_branch_reads(runner)) == 2
    assert events.events == []
    # The pre-read must not leak diagnostics about work the operator did not ask for.
    assert result.diagnostics == ()


def test_rerun_that_retargets_the_version_records_pr_open_for_the_new_version(
    tmp_path: Path,
) -> None:
    """A major update superseding a patch moves the target while the PR stays open."""
    events = _EventLog()
    runner = _runner(
        prs=(_prs(_BRANCH),), branch_files=(_chart_at("0.4.3"), _chart_at("1.0.0"))
    )

    result = _upgrade(tmp_path, runner, events=events)

    assert result.outcome is UpgradeStatus.PR_UPDATED
    assert result.proposed_version == "1.0.0"
    (event,) = events.events
    assert event.build_phase is BuildPhase.PR_OPEN
    assert event.chart_version == "1.0.0"
    # The run-level distinction travels as a property of the run.
    assert event.detail is not None
    assert event.detail["outcome"] == "pr_updated"


def test_a_pull_request_without_a_number_records_no_build_correlation_id(
    tmp_path: Path,
) -> None:
    """Half an identifier would join to nothing."""
    events = _EventLog()
    runner = _runner(
        prs=(_prs(), _prs(_BRANCH, numbered=False)), branch_files=(_chart_at("0.4.3"),)
    )

    result = _upgrade(tmp_path, runner, events=events)

    assert result.pr_number is None
    (event,) = events.events
    assert event.build_correlation_id is None


def test_no_pull_request_after_renovate_is_no_changes(tmp_path: Path) -> None:
    events = _EventLog()
    runner = _runner()

    result = _upgrade(tmp_path, runner, events=events)

    assert result.outcome is UpgradeStatus.NO_CHANGES
    assert result.proposed_version is None
    assert _branch_reads(runner) == []
    assert result.diagnostics == (
        "Renovate completed without an open pull request under "
        "renovate/my-chart/; no eligible update was proposed",
    )
    assert events.events == []


def test_an_unavailable_pull_request_status_is_status_unknown(tmp_path: Path) -> None:
    events = _EventLog()

    result = _upgrade(tmp_path, _runner(prs=(_UNAVAILABLE,)), events=events)

    assert result.outcome is UpgradeStatus.STATUS_UNKNOWN
    assert result.diagnostics == (
        "pull-request status unavailable: gh unavailable",
        "pull-request status unavailable: gh unavailable",
    )
    assert events.events == []


def test_dry_run_records_nothing(tmp_path: Path) -> None:
    """Nothing was pushed, so there is no artifact to report."""
    events = _EventLog()

    _upgrade(tmp_path, _runner(), dry_run=True, events=events)

    assert events.events == []


def test_an_unbumped_wrapper_version_on_the_branch_is_reported(tmp_path: Path) -> None:
    # Renovate records a failed or disallowed post-upgrade command as an
    # artifact error, then opens the pull request and exits zero, so an
    # unbumped wrapper version is the only signal this process can see.
    result = _upgrade(
        tmp_path, _runner(prs=(_prs(_BRANCH),), branch_files=(_chart_at("0.4.2"),))
    )

    assert result.proposed_version == "0.4.2"
    assert any("may not have run" in line for line in result.diagnostics)


def test_an_unreadable_branch_file_is_a_diagnostic_and_records_nothing(tmp_path: Path) -> None:
    """An open PR whose version could not be read must not write "my-chart@None"."""
    events = _EventLog()
    unreadable = Reply(raises=ExternalCommandError("gh api failed"))

    result = _upgrade(
        tmp_path, _runner(prs=(_prs(_BRANCH),), branch_files=(unreadable,)), events=events
    )

    assert result.proposed_version is None
    # A failed read is a reporting gap, not an unknown pull-request status.
    assert result.outcome is UpgradeStatus.PR_UPDATED
    assert any("proposed wrapper version unavailable" in line for line in result.diagnostics)
    assert events.events == []


def test_more_than_one_branch_for_a_chart_is_reported(tmp_path: Path) -> None:
    both = _prs("renovate/my-chart/a", "renovate/my-chart/b")

    result = _upgrade(tmp_path, _runner(prs=(both,), branch_files=(_chart_at("0.4.3"),)))

    assert result.outcome is UpgradeStatus.PR_UPDATED
    assert result.branch == "renovate/my-chart/a"
    assert any("multiple open pull requests" in line for line in result.diagnostics)


def test_a_failed_event_write_does_not_fail_the_upgrade(tmp_path: Path) -> None:
    """The branch is already pushed by the time telemetry runs."""
    events = _EventLog(raises=RuntimeError("cosmos unreachable"))
    runner = _runner(prs=(_prs(), _prs(_BRANCH)), branch_files=(_chart_at("0.4.3"),))

    result = _upgrade(tmp_path, runner, events=events)

    assert result.outcome is UpgradeStatus.PR_OPEN
    assert len(events.events) == 1
