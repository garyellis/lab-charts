"""`validate.select()`: the rows a set of changed files calls for."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.commands import validate
from chart_manager.plumbing.yaml_files import dump_yaml
from chart_manager.shared.workspace import RepositoryWorkspace
from tests.conftest import workspace_for, write_validation_chart

APP = ("app", "dev")
EVERY_ROW = {APP, ("demo", "ci"), ("demo", "dev")}


@pytest.fixture
def workspace(tmp_path: Path) -> RepositoryWorkspace:
    """`demo` (dev, ci) with one per-env trigger and an ignore; `app` (dev) depends on it.

    Any change in `demo` also selects every environment of `app`, which renders it.
    """
    write_validation_chart(
        tmp_path,
        "demo",
        environments={"dev": {"values": ["values.yaml"]}, "ci": {"values": ["values.yaml"]}},
        triggers={"values-dev.yaml": ["dev"]},
        triggerIgnores=["docs/**"],
    )
    app = write_validation_chart(tmp_path, "app")
    (app / "Chart.yaml").write_text(
        dump_yaml(
            {
                "apiVersion": "v2",
                "name": "app",
                "version": "0.1.0",
                "dependencies": [{"name": "demo", "repository": "file://../demo"}],
            }
        )
    )
    return workspace_for(tmp_path, fanout={"validation": ["src/**"]})


@pytest.mark.parametrize(
    ("changes", "rows", "warning"),
    [
        (["charts/demo/values-dev.yaml"], {("demo", "dev"), APP}, None),
        (["charts/demo/values-ci.yaml"], {("demo", "ci"), APP}, None),
        (["charts/demo/templates/cm.yaml"], EVERY_ROW, None),
        (["charts/app/values-ci.yaml"], set(), "matches no trigger"),
        (["charts/app/Chart.yaml"], {APP}, None),
        (["src/chart_manager/x.py"], EVERY_ROW, None),
        (None, EVERY_ROW, None),
        (["charts/demo/docs/a.md"], {APP}, "triggerIgnores"),
        (["README.md"], set(), None),
    ],
    ids=[
        "chart-trigger-plus-dependent",
        "default-trigger-plus-dependent",
        "template-all-envs",
        "default-trigger-env-not-declared",
        "chart-yaml-all-envs",
        "fanout",
        "everything",
        "ignored-dependent-still-selected",
        "outside-charts",
    ],
)
def test_select_picks_the_rows_a_change_calls_for(
    workspace: RepositoryWorkspace,
    changes: list[str] | None,
    rows: set[tuple[str, str]],
    warning: str | None,
) -> None:
    selection = validate.select(changes, workspace=workspace)

    assert {(row.chart, row.env) for row in selection.rows} == rows
    if warning:
        assert any(warning in line for line in selection.warnings)


def test_select_collects_a_broken_chart_and_selects_the_rest(
    tmp_path: Path, workspace: RepositoryWorkspace
) -> None:
    broken = write_validation_chart(tmp_path, "broken")
    (broken / "Chart.yaml").write_text(dump_yaml({"apiVersion": "v2", "name": "other"}))

    selection = validate.select(None, workspace=workspace)

    assert {(row.chart, row.env) for row in selection.rows} == EVERY_ROW
    (error,) = selection.spec_errors
    assert error.startswith("broken:")
    assert validate.Row("demo", "dev", "demo", "lab-dev", {}) in selection.rows
