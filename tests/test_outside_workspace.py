"""Commands that are not about a chart repository work outside one.

`.chart-manager/workspace.yaml` is required, but only by repository-bound
commands. `version`, `doctor`, `event *`, `helmrelease *`, `grafana dashboard
export` and `grafana dashboard lint --path` must never load it, or running
them from `$HOME` -- the usual place to run `doctor` -- would fail with
`WorkspaceNotFoundError` instead of doing their job.

Every test runs from a `tmp_path` with no marker in it or above it, and
asserts on the command's own outcome, not just the absence of one error.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chart_manager.cli import events as events_cli
from chart_manager.cli import grafana as grafana_cli
from chart_manager.composition import Container
from chart_manager.domain.workspace import discover_workspace_root
from chart_manager.plumbing.errors import WorkspaceNotFoundError

from .conftest import FakeCommandRunner, cli

_PASSING_DASHBOARD = {
    "title": "T",
    "uid": "u",
    "schemaVersion": 38,
    "editable": True,
    "panels": [
        {
            "id": 1,
            "title": "p",
            "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
            "targets": [{"expr": "rate(x[$__rate_interval])"}],
        }
    ],
    "templating": {"list": [{"type": "datasource", "name": "DS_PROMETHEUS"}]},
}


@pytest.fixture(autouse=True)
def outside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from a directory that no workspace marker covers."""
    assert discover_workspace_root(tmp_path) is None
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_repository_command_fails_here() -> None:
    """Guard the guard: this directory really is outside any workspace."""
    result = cli("chart", "list")

    assert isinstance(result.exception, WorkspaceNotFoundError)


def test_version() -> None:
    result = cli("version")

    assert result.exit_code == 0, result.output
    assert result.stdout.strip()


def test_doctor_skips_schema_checks_and_probes_git_in_the_working_directory(
    outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeCommandRunner().respond(
        ("git", "rev-parse", "--show-toplevel"), returncode=128, stderr="not a git repository"
    )
    monkeypatch.setattr(Container, "command_runner", lambda _self: runner)
    # Every binary "exists"; what each one answers is the fake runner's call.
    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")

    result = cli("doctor", "-o", "json")

    assert not isinstance(result.exception, WorkspaceNotFoundError), result.output
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    for name in ("schema-policy", "schema-lock", "schema-store"):
        assert checks[name]["status"] == "skipped"
        assert "set CHART_MANAGER_ROOT" in checks[name]["detail"]
    # The git probe still runs, against cwd, and still reports the failure.
    assert checks["git-repository"]["status"] == "failed"
    assert str(outside.resolve()) in checks["git-repository"]["detail"]
    rev_parse = [r for r in runner.records if r.args[:2] == ("git", "rev-parse")]
    assert rev_parse and rev_parse[0].cwd == outside.resolve()


def test_event_list(monkeypatch: pytest.MonkeyPatch) -> None:
    queries: list[Any] = []
    monkeypatch.setattr(events_cli, "_query_events", lambda q: queries.append(q) or [])

    result = cli("event", "list")

    assert result.exit_code == 0, result.output
    assert len(queries) == 1


def test_grafana_dashboard_lint_with_an_explicit_path(outside: Path) -> None:
    dashboard = outside / "ok.json"
    dashboard.write_text(json.dumps(_PASSING_DASHBOARD), encoding="utf-8")

    result = cli("grafana", "dashboard", "lint", "--path", str(dashboard))

    assert result.exit_code == 0, result.output
    assert "1 dashboards passed" in result.stderr


def test_grafana_dashboard_lint_without_a_path_needs_the_workspace() -> None:
    """Discovery reads `spec.chartsDir`, so it is repository-bound."""
    result = cli("grafana", "dashboard", "lint")

    assert isinstance(result.exception, WorkspaceNotFoundError)


def test_grafana_dashboard_export(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = SimpleNamespace(fetch=lambda _request: dict(_PASSING_DASHBOARD))
    monkeypatch.setattr(
        grafana_cli, "_container", lambda: SimpleNamespace(grafana_exporter=lambda: exporter)
    )

    result = cli("grafana", "dashboard", "export", "u", "-o", "json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["uid"] == "u"
