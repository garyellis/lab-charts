"""Chart discovery.

Asserted against a synthetic chart tree, not the repo's own `charts/`
directory -- see tests/conftest.py for why. The one real-tree test at the
bottom asserts containment, so adding a chart cannot turn it red.
"""
from __future__ import annotations

from pathlib import Path

from chart_manager.shared.charts.chart import chart_names
from tests.conftest import CHARTS_DIR

from .conftest import REPO_ROOT, MakeChart


def test_list_charts_discovers_wrappers(chart_root: Path, make_chart: MakeChart) -> None:
    make_chart("tempo")
    make_chart("alloy")
    make_chart("grafana")

    assert chart_names(chart_root / CHARTS_DIR) == ["alloy", "grafana", "tempo"]


def test_list_charts_ignores_directories_without_a_chart_yaml(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alloy")
    # A stray directory under charts/ is not a chart until it has a Chart.yaml.
    (chart_root / "charts" / "scratch").mkdir()
    (chart_root / "charts" / "README.md").write_text("", encoding="utf-8")

    assert chart_names(chart_root / CHARTS_DIR) == ["alloy"]


def test_list_charts_is_empty_when_there_is_no_charts_dir(tmp_path: Path) -> None:
    assert chart_names(tmp_path / CHARTS_DIR) == []


def test_the_repo_chart_tree_loads() -> None:
    """Smoke test over the real charts/ tree: contents, not inventory."""
    names = chart_names(REPO_ROOT / CHARTS_DIR)

    assert names, "the repo should ship at least one chart"
    assert names == sorted(names)
    assert {"alloy", "grafana"} <= set(names)

