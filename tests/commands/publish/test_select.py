"""`publish.select()`: the charts a change set publishes."""

from __future__ import annotations

from pathlib import Path

from chart_manager.commands import publish
from tests.conftest import MakeChart, workspace_for


def test_select_is_the_changed_charts_only_without_fanout_or_deleted_charts(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha")
    make_chart("zeta")
    make_chart("untouched")
    changes = (
        "README.md",
        "charts/zeta/README.md",
        "",
        "  charts/alpha/templates/deployment.yaml  ",
        "charts/removed/Chart.yaml",
        "kind-config.yaml",
    )

    workspace = workspace_for(chart_root, fanout={"chartTest": ["kind-config.yaml"]})

    assert publish.select(changes, workspace=workspace) == ("alpha", "zeta")
