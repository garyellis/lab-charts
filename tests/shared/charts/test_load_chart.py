"""`load_chart`: a chart directory whose Chart.yaml and lifecycle names agree."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from chart_manager.api.v1alpha1.releases import LifecycleRelease
from chart_manager.plumbing.errors import ChartNotFoundError, SpecError
from chart_manager.shared.charts.chart import chart_target, load_chart
from chart_manager.shared.cluster.local_cluster import validate_release
from tests.conftest import MakeChart


def test_a_chart_without_a_lifecycle_file_loads_with_none(make_chart: MakeChart) -> None:
    path = make_chart("demo")
    (path / "chart-lifecycle.yaml").unlink()

    chart = load_chart(path)

    assert (chart.name, chart.lifecycle) == ("demo", None)


def _local_release(root: Path, path: Path) -> None:
    release = LifecycleRelease(type="lifecycle", chart=path.relative_to(root), profile="minimal")
    validate_release(root, release)


# Each way a chart is loaded from a path; all of them apply the one name rule.
_LOADERS: dict[str, Callable[[Path, Path], object]] = {
    "load_chart": lambda _root, path: load_chart(path),
    "chart_target": chart_target,
    "local release": _local_release,
}


def _rename(file: str) -> Callable[[Path], Path]:
    def rename(path: Path) -> Path:
        target = path / file
        target.write_text(target.read_text().replace("name: demo", "name: other"))
        return path

    return rename


def _remove_chart_yaml(path: Path) -> Path:
    (path / "Chart.yaml").unlink()
    return path


def _bad_dependencies(path: Path) -> Path:
    with (path / "Chart.yaml").open("a") as chart_yaml:
        chart_yaml.write("dependencies: wrong\n")
    return path


def _rename_directory(path: Path) -> Path:
    return path.rename(path.with_name("renamed"))


@pytest.mark.parametrize("loader", _LOADERS.values(), ids=_LOADERS.keys())
@pytest.mark.parametrize(
    ("damage", "error"),
    [
        (_rename("Chart.yaml"), SpecError),
        (_rename("chart-lifecycle.yaml"), SpecError),
        (_rename_directory, SpecError),
        (_bad_dependencies, SpecError),
        (_remove_chart_yaml, ChartNotFoundError),
    ],
    ids=[
        "Chart.yaml name",
        "lifecycle name",
        "directory name",
        "bad dependencies",
        "no Chart.yaml",
    ],
)
def test_loading_refuses_a_chart_that_is_malformed_or_missing(
    chart_root: Path,
    make_chart: MakeChart,
    loader: Callable[[Path, Path], object],
    damage: Callable[[Path], Path],
    error: type[Exception],
) -> None:
    path = damage(make_chart("demo"))

    with pytest.raises(error):
        loader(chart_root.resolve(), path.resolve())
