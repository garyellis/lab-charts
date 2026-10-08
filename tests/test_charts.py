"""Chart discovery and values resolution.

Asserted against a synthetic chart tree, not the repo's own `charts/`
directory -- see tests/conftest.py for why. The one real-tree test at the
bottom asserts containment, so adding a chart cannot turn it red.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.plumbing.errors import CapabilityUnavailableError
from chart_manager.plumbing.yaml_files import dump_yaml
from chart_manager.shared.charts.chart import chart_names
from chart_manager.shared.charts.chart_tests import ChartTestCatalog
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


def test_value_paths_are_chart_relative(chart_root: Path, make_chart: MakeChart) -> None:
    make_chart(
        "prometheus-operator",
        profiles={"minimal": {"values": ["values.yaml", "values-ci.yaml"]}},
    )
    catalog = ChartTestCatalog(chart_root, charts_dir=CHARTS_DIR)
    chart = catalog.get("prometheus-operator")

    paths = catalog.value_paths(chart, "minimal")

    chart_dir = (chart_root / "charts" / "prometheus-operator").resolve()
    assert paths == [chart_dir / "values.yaml", chart_dir / "values-ci.yaml"]


def test_chart_test_catalog_requires_chart_manager_configuration(
    chart_root: Path,
) -> None:
    chart_dir = chart_root / "charts" / "common"
    chart_dir.mkdir()
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: common\nversion: 1.2.3\ntype: library\n",
        encoding="utf-8",
    )

    with pytest.raises(
        CapabilityUnavailableError,
        match=r"no chartTest configuration in chart-lifecycle\.yaml",
    ):
        ChartTestCatalog(chart_root, charts_dir=CHARTS_DIR).get("common")


def test_enabled_chart_test_names_exclude_unmanaged_and_disabled_charts(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("enabled")
    unmanaged = make_chart("unmanaged")
    (unmanaged / "chart-lifecycle.yaml").unlink()
    disabled = make_chart("disabled")
    (disabled / "chart-lifecycle.yaml").write_text(
        dump_yaml(
            {
                "apiVersion": "chartmanager.io/v1alpha1",
                "kind": "ChartLifecycle",
                "metadata": {"name": "disabled"},
                "spec": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    section_disabled = make_chart("section-disabled")
    (section_disabled / "chart-lifecycle.yaml").write_text(
        dump_yaml(
            {
                "apiVersion": "chartmanager.io/v1alpha1",
                "kind": "ChartLifecycle",
                "metadata": {"name": "section-disabled"},
                "spec": {
                    "chartTest": {
                        "enabled": False,
                        "profiles": {"minimal": {"namespace": "default"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert ChartTestCatalog(chart_root, charts_dir=CHARTS_DIR).enabled_names() == ["enabled"]


def test_the_repo_chart_tree_loads() -> None:
    """Smoke test over the real charts/ tree: contents, not inventory."""
    names = chart_names(REPO_ROOT / CHARTS_DIR)

    assert names, "the repo should ship at least one chart"
    assert names == sorted(names)
    assert {"alloy", "grafana"} <= set(names)

