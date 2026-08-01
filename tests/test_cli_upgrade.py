"""Focused transport tests for the public upgrade and hidden callback."""

from __future__ import annotations

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
    """Both commands flat, as `cli/main.py` no longer registers them together.

    `upgrade` now lives under `chart` and `upgrade-finalize` stays frozen at
    the root, so they have separate registration functions. This module
    tests transport -- flag shape, encoding, service call -- which is
    independent of where each one is mounted, so it keeps mounting both flat
    and driving them with a plain `CliRunner`.
    """
    app = typer.Typer()
    upgrade_cli.register_upgrade(app)
    upgrade_cli.register_finalize(app)
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
        ["upgrade", "--path", "charts/loki", "--dry-run", "--output", "json"],
    )

    assert result.exit_code == 0
    # Byte-identical, not just structurally equal: `--output json` is a
    # versioned contract consumed by CI steps and jq, so key order, separator
    # spacing, and the trailing newline are all part of it. A refactor that
    # moves where the payload is built must not move a single byte of it.
    assert result.stdout == (
        '{"base":"main","branch":"renovate/loki","chart":"loki",'
        '"current_wrapper_version":"1.2.3",'
        '"diagnostics":["registry lookup retried"],"outcome":"pr_open",'
        '"path":"charts/loki","proposed_wrapper_version":"1.2.4",'
        '"pull_request":{"number":7,"url":"https://example.test/pull/7"},'
        '"repository":"owner/repository","schema_version":1}\n'
    )
    request = service.requests[0]
    assert request.root == tmp_path.resolve()
    assert request.chart_path == Path("charts/loki")
    assert request.dry_run is True


def test_upgrade_text_has_fixed_fields_and_diagnostics(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = _Upgrade(_upgrade_result(Path("charts/loki")))
    monkeypatch.setattr(upgrade_cli, "_make_upgrade_service", lambda _root: service)

    result = CliRunner().invoke(_app(), ["upgrade", "--path", "charts/loki", "--output", "table"])

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
    # `upgrade-finalize` is frozen (design doc §9.5) and its payload is read by
    # the Renovate callback, so pin the whole line byte-for-byte -- including
    # the keys the finalizer cannot populate. The finalizer names the same two
    # wrapper versions `previous_version`/`version`, and they must still land
    # on `current_wrapper_version`/`proposed_wrapper_version` here.
    assert result.stdout == (
        '{"base":null,"branch":null,"chart":"loki",'
        '"current_wrapper_version":"1.2.3","diagnostics":[],'
        '"outcome":"updated","path":"charts/loki",'
        '"proposed_wrapper_version":"2.0.0","pull_request":null,'
        '"repository":null,"schema_version":1}\n'
    )
    request = service.requests[0]
    assert request.repo_root == tmp_path.resolve()
    assert request.update_data is not None
    assert request.update_data["updates"][0]["depName"] == "grafana"


def test_finalize_text_renders_the_keys_the_finalizer_cannot_populate(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The finalizer's null repository/branch/PR must still render as `-`.

    Byte-identical companion to the JSON gate: this is the only path that
    exercises every placeholder branch of the text renderer at once.
    """
    monkeypatch.chdir(tmp_path)
    data_file = tmp_path / "renovate-data.json"
    data_file.write_text('{"updates":[]}', encoding="utf-8")
    service = _Finalize(
        FinalizeResult(
            chart="loki",
            previous_version="1.2.3",
            version="1.2.3",
            bump=None,
            changed=False,
        )
    )
    monkeypatch.setattr(upgrade_cli, "_make_finalize_service", lambda _root: service)

    result = CliRunner().invoke(
        _app(),
        ["upgrade-finalize", "--path", "charts/loki", "--data-file", str(data_file)],
    )

    assert result.exit_code == 0
    assert result.stdout == (
        "repository: -\n"
        "base: -\n"
        "chart: loki\n"
        "path: charts/loki\n"
        "current wrapper version: 1.2.3\n"
        "proposed wrapper version: 1.2.3\n"
        "branch: -\n"
        "outcome: unchanged\n"
        "pull request: -\n"
        "diagnostics:\n"
        "- none\n"
    )


def test_unknown_output_is_rejected_before_service_call(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    called = False

    def make(_root):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        return _Upgrade(_upgrade_result(Path("charts/loki")))

    monkeypatch.setattr(upgrade_cli, "_make_upgrade_service", make)

    result = CliRunner().invoke(_app(), ["upgrade", "--path", "charts/loki", "--output", "yaml"])

    assert result.exit_code == 2
    assert "unknown output: yaml" in result.output
    assert called is False
