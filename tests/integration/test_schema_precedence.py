"""Which schema real kubeconform applies, and how a broken schema or resource is reported."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_manager.commands import validate
from chart_manager.commands.validate.run import run
from chart_manager.plumbing.commands import SubprocessRunner
from chart_manager.shared.workspace import RepositoryWorkspace
from tests.conftest import write_validation_chart
from tests.integration.conftest import require

pytestmark = pytest.mark.integration

WIDGET = "apiVersion: example.io/v1\nkind: Widget\nmetadata: {name: demo}\n"


def widget_chart(root: Path, manifest: str = WIDGET, **validation: object) -> None:
    chart = write_validation_chart(
        root, "demo", schemaLocations=["local/{{.ResourceKind}}.json"], **validation
    )
    (chart / "templates").mkdir()
    (chart / "templates/widget.yaml").write_text(manifest)
    (root / "local").mkdir(exist_ok=True)


def schema_check(workspace: RepositoryWorkspace) -> validate.CheckResult:
    outcome = run(
        validate.ValidateRequest(
            out=workspace.root / "out", charts=("demo",), checks=frozenset({"render", "schema"})
        ),
        workspace=workspace,
        runner=SubprocessRunner(),
    )
    return outcome.rows[0].checks["schema"]


def test_the_charts_own_schema_wins_over_the_upstream_catalog(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    require("helm", "kubeconform", "git")
    widget_chart(tmp_path, WIDGET + "source: upstream\n")
    local = tmp_path / "local/widget.json"
    local.write_text(json.dumps({"type": "object", "properties": {"source": {"enum": ["local"]}}}))

    assert schema_check(schema_workspace).status == "failed"
    # Without the chart's schema, the permissive pinned catalog schema applies.
    local.unlink()
    assert schema_check(schema_workspace).status == "passed"


@pytest.mark.parametrize("ignore", [(), ("Widget",)])
def test_an_unreadable_schema_is_an_error_even_for_an_ignored_kind(
    tmp_path: Path, schema_workspace: RepositoryWorkspace, ignore: tuple[str, ...]
) -> None:
    """Truncated JSON. A schema that parses but is invalid makes kubeconform try the next location."""
    require("helm", "kubeconform", "git")
    widget_chart(tmp_path, ignoreMissingSchemas=list(ignore))
    (tmp_path / "local/widget.json").write_text('{"type":')

    result = schema_check(schema_workspace)

    assert result.status == "error"
    assert "chart-manager schemas sync" in result.detail


def test_a_resource_without_a_kind_is_a_chart_failure(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    require("helm", "kubeconform", "git")
    widget_chart(tmp_path, "apiVersion: example.io/v1\nmetadata: {name: demo}\n")

    result = schema_check(schema_workspace)

    assert result.status == "failed", result.detail
    assert "chart-manager schemas sync" not in result.detail
