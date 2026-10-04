"""`load_chart`: a chart directory whose Chart.yaml and lifecycle names agree."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.plumbing.errors import ChartNotFoundError, SpecError
from chart_manager.shared.charts.chart import load_chart
from tests.conftest import MakeChart


def test_a_chart_without_a_lifecycle_file_loads_with_none(make_chart: MakeChart) -> None:
    path = make_chart("demo")
    (path / "chart-lifecycle.yaml").unlink()

    chart = load_chart(path)

    assert (chart.name, chart.lifecycle) == ("demo", None)


@pytest.mark.parametrize(
    ("file", "old", "new", "error"),
    [
        ("Chart.yaml", "name: demo", "name: other", SpecError),
        ("chart-lifecycle.yaml", "name: demo", "name: other", SpecError),
        ("Chart.yaml", None, None, ChartNotFoundError),
    ],
)
def test_load_chart_refuses_a_chart_whose_names_disagree_or_that_is_missing(
    make_chart: MakeChart, file: str, old: str | None, new: str | None, error: type[Exception]
) -> None:
    path = make_chart("demo")
    target: Path = path / file
    if old is None:
        target.unlink()
    else:
        target.write_text(target.read_text().replace(old, new or ""))

    with pytest.raises(error):
        load_chart(path)
