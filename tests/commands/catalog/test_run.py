"""`list_charts` and `show_chart` over a `tmp_path` chart tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.commands.catalog import run as catalog
from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.yaml_files import dump_yaml, parse_yaml
from tests.conftest import MakeChart, workspace_for


def test_show_chart_rejects_a_lifecycle_identity_mismatch(
    chart_root: Path, make_chart: MakeChart
) -> None:
    chart = make_chart("actual")
    lifecycle = parse_yaml((chart / "chart-lifecycle.yaml").read_text())
    lifecycle["metadata"]["name"] = "other"
    (chart / "chart-lifecycle.yaml").write_text(dump_yaml(lifecycle), encoding="utf-8")

    with pytest.raises(SpecError, match=r"metadata\.name 'other'"):
        catalog.show_chart(workspace_for(chart_root), "actual")
