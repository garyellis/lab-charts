"""`list_charts` and `show_chart` over a `tmp_path` chart tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.commands.catalog import run as catalog
from chart_manager.commands.catalog.wire import catalog_to_dict
from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.yaml_files import dump_yaml, parse_yaml
from tests.conftest import MakeChart, workspace_for


def test_list_charts_is_the_versioned_catalog_document(
    chart_root: Path, make_chart: MakeChart
) -> None:
    """Asserted key by key: a rename has to be a deliberate edit, not a re-blessed golden."""
    make_chart("alloy", profiles={"minimal": {}, "telemetry": {}})

    document = catalog_to_dict(catalog.list_charts(workspace_for(chart_root)))

    assert document["charts"] == [
        {
            "name": "alloy",
            "type": "application",
            "version": "0.1.0",
            "dependencies": [],
            "lifecycle": "enabled",
            "manifest_validation": "absent",
            "chart_test": "enabled",
            "profiles": ["minimal", "telemetry"],
            "error": None,
        }
    ]


def test_list_charts_retains_invalid_config_for_operator_visibility(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("broken")
    (chart / "chart-lifecycle.yaml").write_text("version: [wrong\n", encoding="utf-8")

    entry = catalog.list_charts(workspace_for(chart_root))[0]

    assert entry.name == "broken"
    assert entry.lifecycle_status == "invalid"
    assert entry.error is not None


def test_a_lifecycle_identity_mismatch_is_invalid_in_list_and_an_error_in_show(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("actual")
    lifecycle = parse_yaml((chart / "chart-lifecycle.yaml").read_text())
    lifecycle["metadata"]["name"] = "other"
    (chart / "chart-lifecycle.yaml").write_text(dump_yaml(lifecycle), encoding="utf-8")
    workspace = workspace_for(chart_root)

    entry = catalog.list_charts(workspace)[0]

    assert entry.lifecycle_status == "invalid"
    assert entry.error is not None
    assert "metadata.name 'other'" in entry.error
    assert "Chart.yaml name 'actual'" in entry.error

    with pytest.raises(SpecError, match=r"metadata\.name 'other'"):
        catalog.show_chart(workspace, "actual")

