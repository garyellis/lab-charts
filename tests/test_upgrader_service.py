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
    assert first.branch == second.branch == "renovate/my-chart"
    assert first.group == second.group == "chart-manager:my-chart"
    assert first.runtime_overlay["includePaths"] == ["charts/my-chart/**"]
    assert first.runtime_overlay["packageRules"][0]["matchFileNames"] == ["charts/my-chart/**"]
    assert first.runtime_overlay["enabledManagers"] == ["helmv3", "helm-values", "custom.regex"]
    assert first.runtime_overlay["branchPrefix"] == "renovate/"
    assert first.runtime_overlay["lockFileMaintenance"] == {"enabled": False}
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
            None,
            SimpleNamespace(url="https://example.test/pull/7", number=7),
        )
    )
    service = UpgradeService(
        Adapter(),
        factory,
        pull_request_lookup=lambda branch: next(responses),
        repository="owner/repository",
        base="main",
    )

    opened = service.upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert opened.outcome == "pr_open"
    assert opened.pr_url == "https://example.test/pull/7"
    assert opened.pr_number == 7
    assert opened.repository == "owner/repository"
    assert opened.base == "main"


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
