from pathlib import Path
from types import SimpleNamespace

import pytest

from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.services.upgrader import (
    UpgradeError,
    UpgradeRequest,
    UpgradeService,
    build_upgrade_plan,
)


def _chart(tmp_path: Path) -> Path:
    chart = tmp_path / "charts" / "my-chart"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: my-chart\nversion: 0.4.2\n", encoding="utf-8"
    )
    return chart


def test_plan_has_deterministic_branch_group_and_scoped_overlay(tmp_path: Path) -> None:
    chart = _chart(tmp_path)
    first = build_upgrade_plan(tmp_path, chart)
    second = build_upgrade_plan(tmp_path, Path("my-chart"))
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


def test_service_uses_injected_adapter_and_factory(tmp_path: Path) -> None:
    chart = _chart(tmp_path)
    calls: list[object] = []

    class Adapter:
        def run(self, request: object) -> object:
            calls.append(request)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def factory(plan: object, *, dry_run: bool) -> object:
        return (plan, dry_run)

    result = UpgradeService(Adapter(), factory).upgrade(
        UpgradeRequest(root=tmp_path, chart_path=chart, dry_run=True)
    )
    assert calls
    assert result.outcome == "dry_run"
    assert result.current_version == "0.4.2"
    assert result.to_dict()["chart_path"] == str(chart)


def test_service_projects_new_and_existing_pull_request_status(tmp_path: Path) -> None:
    chart = _chart(tmp_path)

    class Adapter:
        def run(self, request: object) -> object:
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    def factory(plan: object, *, dry_run: bool) -> object:
        return plan

    responses = iter(
        (
            (),
            (
                SimpleNamespace(
                    url="https://example.test/pull/7",
                    number=7,
                    branch="renovate/my-chart/my-chart",
                ),
            ),
        )
    )
    service = UpgradeService(
        Adapter(),
        factory,
        pull_request_lookup=lambda prefix: next(responses),
        repository="owner/repository",
        base="main",
    )

    opened = service.upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert opened.outcome == "pr_open"
    assert opened.pr_url == "https://example.test/pull/7"
    assert opened.pr_number == 7
    assert opened.repository == "owner/repository"
    assert opened.base == "main"
    # Reported from the PR's head ref, not re-derived from Renovate's naming.
    assert opened.branch == "renovate/my-chart/my-chart"


def _pr(branch: str = "renovate/my-chart/my-chart") -> SimpleNamespace:
    return SimpleNamespace(url="https://example.test/pull/7", number=7, branch=branch)


class _Ok:
    def run(self, request: object) -> object:
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_proposed_version_is_read_back_from_the_upgrade_branch(tmp_path: Path) -> None:
    chart = _chart(tmp_path)
    reads: list[tuple[str, str]] = []

    def read(path: str, ref: str) -> str:
        reads.append((path, ref))
        return "apiVersion: v2\nname: my-chart\nversion: 0.4.3\n"

    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: (_pr(),),
        branch_file_reader=read,
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.proposed_version == "0.4.3"
    assert result.changed is True
    assert reads == [("charts/my-chart/Chart.yaml", "renovate/my-chart/my-chart")]
    assert result.diagnostics == ()


def test_unbumped_wrapper_version_on_the_branch_is_reported(tmp_path: Path) -> None:
    chart = _chart(tmp_path)

    # Renovate records a failed or disallowed post-upgrade command as an
    # artifact error, then opens the pull request and exits zero, so an
    # unbumped wrapper version is the only signal this process can see.
    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: (_pr(),),
        branch_file_reader=lambda path, ref: "name: my-chart\nversion: 0.4.2\n",
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.proposed_version == "0.4.2"
    assert any("may not have run" in line for line in result.diagnostics)


def test_unreadable_branch_file_degrades_to_a_diagnostic(tmp_path: Path) -> None:
    chart = _chart(tmp_path)

    def unavailable(path: str, ref: str) -> str:
        raise ExternalCommandError("gh api failed")

    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: (_pr(),),
        branch_file_reader=unavailable,
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.proposed_version is None
    # A failed read is a reporting gap, not an unknown pull-request status.
    assert result.outcome == "pr_updated"
    assert any("proposed wrapper version unavailable" in line for line in result.diagnostics)


def test_no_branch_read_without_a_pull_request(tmp_path: Path) -> None:
    chart = _chart(tmp_path)

    def unexpected(path: str, ref: str) -> str:
        raise AssertionError("must not read a branch when no pull request is open")

    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: (),
        branch_file_reader=unexpected,
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.outcome == "no_changes"
    assert result.proposed_version is None


def test_service_reports_drift_when_a_chart_holds_more_than_one_branch(
    tmp_path: Path,
) -> None:
    chart = _chart(tmp_path)

    class Adapter:
        def run(self, request: object) -> object:
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    found = (
        SimpleNamespace(url="https://example.test/pull/7", number=7, branch="renovate/my-chart/a"),
        SimpleNamespace(url="https://example.test/pull/9", number=9, branch="renovate/my-chart/b"),
    )
    result = UpgradeService(
        Adapter(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: found,
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.outcome == "pr_updated"
    assert any("multiple open pull requests" in line for line in result.diagnostics)


def test_service_does_not_report_no_changes_when_pr_status_is_unavailable(
    tmp_path: Path,
) -> None:
    chart = _chart(tmp_path)

    class Adapter:
        def run(self, request: object) -> object:
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    def unavailable(branch: str) -> object:
        raise ExternalCommandError("gh unavailable")

    result = UpgradeService(
        Adapter(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=unavailable,
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.outcome == "status_unknown"
    assert result.diagnostics == (
        "pull-request status unavailable: gh unavailable",
        "pull-request status unavailable: gh unavailable",
    )


def test_service_rejects_relevant_uncommitted_inputs_before_renovate(tmp_path: Path) -> None:
    chart = _chart(tmp_path)
    called = False

    class Adapter:
        def run(self, request: object) -> object:
            nonlocal called
            called = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(UpgradeError, match="uncommitted changes"):
        UpgradeService(
            Adapter(),
            lambda plan, *, dry_run: plan,
            relevant_changes=lambda paths: ("charts/my-chart/values.yaml",),
        ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert called is False
