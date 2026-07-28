"""The managed chart root is one configuration value across every subsystem."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chart_manager.integrations.git import Git
from chart_manager.services.domain.charts import ChartRepository
from chart_manager.services.grafana.dashboard_lint import discover_dashboards
from chart_manager.services.manifest_validation.planner import build_worklist
from chart_manager.services.upgrader.paths import resolve_chart_path
from chart_manager.settings import RepositoryLayout, Settings
from tests.conftest import FakeCommandRunner

CUSTOM_CHARTS_DIR = Path("deploy/helm")


def _write_chart(root: Path, name: str) -> Path:
    chart = root / CUSTOM_CHARTS_DIR / name
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        f"apiVersion: v2\nname: {name}\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    return chart


def test_settings_default_and_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHART_MANAGER_CHARTS_DIR", raising=False)
    assert Settings().charts_dir == Path("charts")

    monkeypatch.setenv("CHART_MANAGER_CHARTS_DIR", "deploy/helm")
    assert Settings().charts_dir == CUSTOM_CHARTS_DIR
    assert Settings(charts_dir=Path("explicit/charts")).charts_dir == Path(
        "explicit/charts"
    )


@pytest.mark.parametrize(
    "value",
    [Path("."), Path("../charts"), Path("deploy/../charts"), Path("/tmp/charts")],
)
def test_settings_rejects_unsafe_chart_directories(value: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(charts_dir=value)


def test_repository_layout_parses_nested_chart_prefix(tmp_path: Path) -> None:
    layout = RepositoryLayout(root=tmp_path, charts_dir=CUSTOM_CHARTS_DIR)

    assert layout.chart_name_from_repo_path("deploy/helm/loki/values.yaml") == "loki"
    assert layout.chart_name_from_repo_path("charts/loki/values.yaml") is None
    assert layout.repo_chart_path("loki", "values.yaml") == Path(
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

    repository = ChartRepository(tmp_path, charts_dir=CUSTOM_CHARTS_DIR)
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
    assert Git(
        tmp_path, runner=runner, charts_dir=CUSTOM_CHARTS_DIR
    ).changed_charts() == ["demo"]

    _, resolved, _ = resolve_chart_path(
        tmp_path,
        Path("demo"),
        charts_dir=CUSTOM_CHARTS_DIR,
    )
    assert resolved == chart.resolve()
    assert discover_dashboards(
        tmp_path, charts_dir=CUSTOM_CHARTS_DIR
    ) == [dashboard]


def test_manifest_planner_classifies_changes_under_custom_root(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path, "demo")
    (chart / "chart-lifecycle.yaml").write_text(
        """\
apiVersion: lifecycle.cmg.io/v1alpha1
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

    result = build_worklist(
        root=tmp_path,
        changed_files=["deploy/helm/demo/values.yaml"],
        charts_dir=CUSTOM_CHARTS_DIR,
    )

    assert [(row.chart, row.env) for row in result.rows] == [("demo", "dev")]
    assert result.spec_errors == ()
