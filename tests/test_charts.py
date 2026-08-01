"""ChartRepository discovery and values resolution.

Asserted against a synthetic chart tree, not the repo's own `charts/`
directory -- see tests/conftest.py for why. The one real-tree test at the
bottom asserts containment, so adding a chart cannot turn it red.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from chart_manager.plumbing.errors import CapabilityUnavailableError, SpecError
from chart_manager.plumbing.exit_codes import EXIT_SPEC
from chart_manager.services.chart_catalog import ChartCatalogService
from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.domain.charts import ChartRepository

from .conftest import REPO_ROOT, MakeChart, cli


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
    catalog = ClusterTestCatalog(chart_root)
    chart = catalog.get("prometheus-operator")

    paths = catalog.value_paths(chart, "minimal")

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


def test_cluster_test_catalog_requires_chart_manager_configuration(
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
        match=r"no clusterTest configuration in chart-lifecycle\.yaml",
    ):
        ClusterTestCatalog(chart_root).get("common")


def test_enabled_cluster_test_names_exclude_unmanaged_and_disabled_charts(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("enabled")
    unmanaged = make_chart("unmanaged")
    (unmanaged / "chart-lifecycle.yaml").unlink()
    disabled = make_chart("disabled")
    (disabled / "chart-lifecycle.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "lifecycle.cmg.io/v1alpha1",
                "kind": "ChartLifecycle",
                "metadata": {"name": "disabled"},
                "spec": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    section_disabled = make_chart("section-disabled")
    (section_disabled / "chart-lifecycle.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "lifecycle.cmg.io/v1alpha1",
                "kind": "ChartLifecycle",
                "metadata": {"name": "section-disabled"},
                "spec": {
                    "clusterTest": {
                        "enabled": False,
                        "profiles": {"minimal": {}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert ClusterTestCatalog(chart_root).enabled_names() == ["enabled"]


def test_chart_catalog_retains_invalid_config_for_operator_visibility(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("broken")
    (chart / "chart-lifecycle.yaml").write_text("version: [wrong\n", encoding="utf-8")

    entry = ChartCatalogService(chart_root).list_entries()[0]

    assert entry.name == "broken"
    assert entry.lifecycle_status == "invalid"
    assert entry.error is not None


def test_chart_catalog_rejects_lifecycle_identity_mismatch(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("actual")
    lifecycle = yaml.safe_load((chart / "chart-lifecycle.yaml").read_text())
    lifecycle["metadata"]["name"] = "other"
    (chart / "chart-lifecycle.yaml").write_text(
        yaml.safe_dump(lifecycle),
        encoding="utf-8",
    )

    entry = ChartCatalogService(chart_root).list_entries()[0]

    assert entry.lifecycle_status == "invalid"
    assert entry.error is not None
    assert "metadata.name 'other'" in entry.error
    assert "Chart.yaml name 'actual'" in entry.error

    with pytest.raises(SpecError, match=r"metadata\.name 'other'"):
        ChartCatalogService(chart_root).get_lifecycle("actual")


def test_charts_list_returns_nonzero_after_rendering_invalid_config(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("broken")
    (chart / "chart-lifecycle.yaml").write_text("version: [wrong\n", encoding="utf-8")

    result = cli("chart", "list", "--root", str(chart_root))

    # 3, not 1: an unparseable `chart-lifecycle.yaml` is a spec error in the
    # exit-code table, and `chart list` is the command most likely to be the
    # first to notice one.
    assert result.exit_code == EXIT_SPEC
    assert "broken" in result.stdout
    assert "invalid" in result.stdout


def test_charts_lifecycle_prints_the_normalized_envelope(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("alloy")

    result = cli("chart", "show", "alloy", "--root", str(chart_root))

    assert result.exit_code == 0
    assert '"apiVersion": "lifecycle.cmg.io/v1alpha1"' in result.stdout
    assert '"kind": "ChartLifecycle"' in result.stdout
    assert '"clusterTest"' in result.stdout
    assert '"enabled": true' in result.stdout


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
