"""ChartRepository discovery and values resolution.

Asserted against a synthetic chart tree, not the repo's own `charts/`
directory -- see tests/conftest.py for why. The one real-tree test at the
bottom asserts containment, so adding a chart cannot turn it red.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.plumbing.errors import SpecError
from chart_manager.services.domain.charts import ChartRepository

from .conftest import REPO_ROOT, MakeChart


def test_list_charts_discovers_wrappers(chart_root: Path, make_chart: MakeChart) -> None:
    make_chart("tempo")
    make_chart("alloy")
    make_chart("grafana")

    repository = ChartRepository(chart_root)

    assert repository.list_names() == ["alloy", "grafana", "tempo"]


def test_list_charts_ignores_directories_without_a_chart_yaml(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alloy")
    # A stray directory under charts/ is not a chart until it has a Chart.yaml.
    (chart_root / "charts" / "scratch").mkdir()
    (chart_root / "charts" / "README.md").write_text("", encoding="utf-8")

    assert ChartRepository(chart_root).list_names() == ["alloy"]


def test_list_charts_is_empty_when_there_is_no_charts_dir(tmp_path: Path) -> None:
    assert ChartRepository(tmp_path).list_names() == []


def test_value_paths_are_chart_relative(chart_root: Path, make_chart: MakeChart) -> None:
    make_chart(
        "prometheus-operator",
        profiles={"minimal": {"values": ["values.yaml", "values-ci.yaml"]}},
    )
    repository = ChartRepository(chart_root)
    chart = repository.get_managed("prometheus-operator")

    paths = repository.value_paths(chart, "minimal")

    chart_dir = (chart_root / "charts" / "prometheus-operator").resolve()
    assert paths == [chart_dir / "values.yaml", chart_dir / "values-ci.yaml"]


def test_get_loads_library_chart_without_test_spec(chart_root: Path) -> None:
    chart_dir = chart_root / "charts" / "common"
    chart_dir.mkdir()
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: common\nversion: 1.2.3\ntype: library\n",
        encoding="utf-8",
    )

    chart = ChartRepository(chart_root).get("common")

    assert chart.name == "common"
    assert chart.metadata.version == "1.2.3"
    assert chart.metadata.chart_type == "library"


def test_get_managed_requires_test_spec(chart_root: Path) -> None:
    chart_dir = chart_root / "charts" / "common"
    chart_dir.mkdir()
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: common\nversion: 1.2.3\ntype: library\n",
        encoding="utf-8",
    )

    with pytest.raises(SpecError, match=r"test-spec\.yaml"):
        ChartRepository(chart_root).get_managed("common")


def test_get_rejects_invalid_dependency_shape(chart_root: Path) -> None:
    chart_dir = chart_root / "charts" / "broken"
    chart_dir.mkdir()
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: broken\nversion: 1.2.3\ndependencies: wrong\n",
        encoding="utf-8",
    )

    with pytest.raises(SpecError, match="'dependencies' must be a list"):
        ChartRepository(chart_root).get("broken")


def test_the_repo_chart_tree_loads() -> None:
    """Smoke test over the real charts/ tree: contents, not inventory."""
    names = ChartRepository(REPO_ROOT).list_names()

    assert names, "the repo should ship at least one chart"
    assert names == sorted(names)
    assert {"alloy", "grafana"} <= set(names)
