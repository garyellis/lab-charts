"""Focused transport tests for the public upgrade and hidden callback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from typer.testing import CliRunner

from chart_manager.cli import upgrade as upgrade_cli
from chart_manager.services.upgrader import FinalizeResult, UpgradeResult


class _Upgrade:
    def __init__(self, result: UpgradeResult) -> None:
        self.result = result
        self.requests: list[Any] = []

    def upgrade(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return self.result


class _Finalize:
    def __init__(self, result: FinalizeResult) -> None:
        self.result = result
        self.requests: list[Any] = []

    def finalize(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return self.result


def _app() -> typer.Typer:
    app = typer.Typer()
    upgrade_cli.register(app)
    return app


def _upgrade_result(path: Path) -> UpgradeResult:
    return UpgradeResult(
        chart="loki",
        chart_path=path,
        current_version="1.2.3",
        proposed_version="1.2.4",
        branch="renovate/loki",
        group="chart-manager:loki",
        outcome="pr_open",
        diagnostics=("registry lookup retried",),
        pr_url="https://example.test/pull/7",
        pr_number=7,
        repository="owner/repository",
        base="main",
    )


def test_upgrade_json_is_stable_and_request_preserves_flags(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    service = _Upgrade(_upgrade_result(Path("charts/loki")))
    monkeypatch.setattr(upgrade_cli, "_make_upgrade_service", lambda _root: service)

    result = CliRunner().invoke(
        _app(),
        ["upgrade", "--path", "charts/loki", "--dry-run", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "base": "main",
        "branch": "renovate/loki",
        "chart": "loki",
        "current_wrapper_version": "1.2.3",
        "diagnostics": ["registry lookup retried"],
        "outcome": "pr_open",
        "path": "charts/loki",
        "proposed_wrapper_version": "1.2.4",
        "pull_request": {
            "number": 7,
            "url": "https://example.test/pull/7",
        },
        "repository": "owner/repository",
        "schema_version": 1,
    }
    request = service.requests[0]
    assert request.root == tmp_path.resolve()
    assert request.chart_path == Path("charts/loki")
    assert request.dry_run is True


def test_upgrade_text_has_fixed_fields_and_diagnostics(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = _Upgrade(_upgrade_result(Path("charts/loki")))
    monkeypatch.setattr(upgrade_cli, "_make_upgrade_service", lambda _root: service)

    result = CliRunner().invoke(_app(), ["upgrade", "--path", "charts/loki"])

    assert result.exit_code == 0
    assert result.stdout == (
        "repository: owner/repository\n"
        "base: main\n"
        "chart: loki\n"
        "path: charts/loki\n"
        "current wrapper version: 1.2.3\n"
        "proposed wrapper version: 1.2.4\n"
        "branch: renovate/loki\n"
        "outcome: pr_open\n"
        "pull request: #7 https://example.test/pull/7\n"
        "diagnostics:\n"
        "- registry lookup retried\n"
    )


def test_finalize_is_hidden_and_reads_callback_data_from_environment(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    data_file = tmp_path / "renovate-data.json"
    data_file.write_text(
        '{"updates":[{"depName":"grafana","currentVersion":"1.0.0",'
        '"newVersion":"2.0.0","datasource":"docker"}]}',
        encoding="utf-8",
    )
    service = _Finalize(
        FinalizeResult(
            chart="loki",
            previous_version="1.2.3",
            version="2.0.0",
            bump="major",
            changed=True,
        )
    )
    monkeypatch.setattr(upgrade_cli, "_make_finalize_service", lambda _root: service)
    runner = CliRunner()

    help_result = runner.invoke(_app(), ["--help"])
    result = runner.invoke(
        _app(),
        ["upgrade-finalize", "--path", "charts/loki", "--format", "json"],
        env={"RENOVATE_POST_UPGRADE_COMMAND_DATA_FILE": str(data_file)},
    )

    assert "upgrade-finalize" not in help_result.stdout
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["current_wrapper_version"] == "1.2.3"
    assert payload["proposed_wrapper_version"] == "2.0.0"
    assert payload["outcome"] == "updated"
    request = service.requests[0]
    assert request.repo_root == tmp_path.resolve()
    assert request.update_data is not None
    assert request.update_data["updates"][0]["depName"] == "grafana"


def test_unknown_format_is_rejected_before_service_call(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    called = False

    def make(_root):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        return _Upgrade(_upgrade_result(Path("charts/loki")))

    monkeypatch.setattr(upgrade_cli, "_make_upgrade_service", make)

    result = CliRunner().invoke(_app(), ["upgrade", "--path", "charts/loki", "--format", "yaml"])

    assert result.exit_code == 2
    assert "unknown format: yaml" in result.output
    assert called is False
