"""The managed chart root is one workspace value across every subsystem.

`spec.chartsDir` in `.chart-manager/workspace.yaml` is the only place the
chart root is configured. A nested value (`deploy/helm`) is the case that
catches a subsystem quietly assuming `charts/`, so every consumer is
asserted against one here: change mapping, git, the planner, upgrade path
resolution, dashboard discovery, and the services the container builds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.commands import validate
from chart_manager.commands.catalog import run as catalog
from chart_manager.commands.grafana.dashboard_lint import discover_dashboards
from chart_manager.commands.local.targets import LocalTargetResolver
from chart_manager.commands.upgrade.paths import resolve_chart_path
from chart_manager.commands.validate.render_dir import render_dir_state
from chart_manager.composition import Container
from chart_manager.integrations.git import Git
from chart_manager.settings import Settings
from chart_manager.shared.charts.chart import ChartRepository
from tests.conftest import FakeCommandRunner, workspace_for, write_workspace

CUSTOM_CHARTS_DIR = Path("deploy/helm")


def _write_chart(root: Path, name: str) -> Path:
    chart = root / CUSTOM_CHARTS_DIR / name
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        f"apiVersion: v2\nname: {name}\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    return chart


def test_log_level_has_case_insensitive_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHART_MANAGER_LOG_LEVEL", raising=False)
    assert Settings().log_level == "INFO"

    monkeypatch.setenv("CHART_MANAGER_LOG_LEVEL", "debug")
    assert Settings().log_level == "DEBUG"


def test_log_format_has_case_insensitive_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHART_MANAGER_LOG_FORMAT", raising=False)
    assert Settings().log_format == "text"

    monkeypatch.setenv("CHART_MANAGER_LOG_FORMAT", "JSON")
    assert Settings().log_format == "json"


def test_offline_environment_variable_has_no_settings_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHART_MANAGER_OFFLINE", "1")
    assert "offline" not in Settings.model_fields
    assert not hasattr(Settings(), "offline")


def test_workspace_parses_nested_chart_prefix(tmp_path: Path) -> None:
    workspace = workspace_for(tmp_path, chartsDir=CUSTOM_CHARTS_DIR)

    assert workspace.chart_name_from_repo_path("deploy/helm/loki/values.yaml") == "loki"
    assert workspace.chart_name_from_repo_path("charts/loki/values.yaml") is None
    assert workspace.repo_chart_path("loki", "values.yaml") == Path(
        "deploy/helm/loki/values.yaml"
    )


def test_discovery_git_upgrade_and_dashboards_share_custom_root(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path, "demo")
    dashboard = (
        tmp_path
        / CUSTOM_CHARTS_DIR
        / "grafana-dashboards"
        / "dashboards"
        / "overview.json"
    )
    dashboard.parent.mkdir(parents=True)
    dashboard.write_text("{}", encoding="utf-8")

    workspace = workspace_for(tmp_path, chartsDir=CUSTOM_CHARTS_DIR)
    repository = ChartRepository(tmp_path, charts_dir=workspace.spec.charts_dir)
    assert repository.list_names() == ["demo"]
    assert repository.get("demo").path == chart

    runner = (
        FakeCommandRunner()
        .respond(("git", "rev-parse"), returncode=0)
        .respond(
            ("git", "diff"),
            stdout="deploy/helm/demo/values.yaml\ncharts/ignored/values.yaml\n",
        )
    )
    changed = Git(tmp_path, runner=runner).changed_files()
    assert {workspace.chart_name_from_repo_path(path) for path in changed} - {None} == {"demo"}

    _, resolved, _ = resolve_chart_path(
        tmp_path,
        Path("demo"),
        charts_dir=workspace.spec.charts_dir,
    )
    assert resolved == chart.resolve()
    assert discover_dashboards(workspace=workspace) == [dashboard]


def test_manifest_planner_classifies_changes_under_custom_root(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path, "demo")
    (chart / "chart-lifecycle.yaml").write_text(
        """\
apiVersion: chartmanager.io/v1alpha1
kind: ChartLifecycle
metadata:
  name: demo
spec:
  validation:
    releaseName: demo
    environments:
      dev:
        namespace: lab-dev
        values: [values.yaml]
    triggers:
      values.yaml: [dev]
""",
        encoding="utf-8",
    )
    (chart / "values.yaml").write_text("", encoding="utf-8")

    result = validate.select(
        ["deploy/helm/demo/values.yaml"],
        workspace=workspace_for(tmp_path, chartsDir=CUSTOM_CHARTS_DIR),
    )

    assert [(row.chart, row.env) for row in result.rows] == [("demo", "dev")]
    assert result.spec_errors == ()


def test_one_workspace_reaches_the_services_the_cli_used_to_build(tmp_path: Path) -> None:
    """One container's workspace answers for every service it builds.

    Each of these three was once constructed at the surface from a layout of
    its own, so the container's configuration was not what answered the
    question. Write a nested `chartsDir` once and `chart list`, `local up`
    and `chart cache clean` all address it.
    """
    _write_chart(tmp_path, "demo")
    write_workspace(tmp_path, chartsDir=CUSTOM_CHARTS_DIR.as_posix())
    container = Container(Settings())

    charts = catalog.list_charts(container.workspace(tmp_path))
    assert [entry.name for entry in charts] == ["demo"]

    resolved = _local_targets(container, tmp_path).resolve("deploy/helm/demo")
    assert resolved.path == (tmp_path / CUSTOM_CHARTS_DIR / "demo").resolve()

    assert render_dir_state(container.workspace(tmp_path)).path.is_relative_to(tmp_path)


def _local_targets(container: Container, root: Path) -> LocalTargetResolver:
    workspace = container.workspace(root)
    return LocalTargetResolver(workspace.root, local_config=workspace.spec.local_cluster)
