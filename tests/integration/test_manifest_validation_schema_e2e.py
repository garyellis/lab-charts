"""`validate.run()` with real helm and kubeconform: render, then the schema check."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from chart_manager.commands import validate
from chart_manager.plumbing.commands import SubprocessRunner
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.yaml_files import dump_yaml
from chart_manager.shared.workspace import RepositoryWorkspace
from tests.conftest import crd_manifest, write_validation_chart
from tests.integration.conftest import FIXTURES, fixture_chart, require

pytestmark = pytest.mark.integration

SCHEMA_LOCATION = "schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
SCHEMA_ONLY = frozenset({"render", "schema"})


def check(workspace: RepositoryWorkspace, *charts: str) -> validate.ValidateOutcome:
    return validate.run(
        validate.ValidateRequest(out=workspace.root / "out", charts=charts, checks=SCHEMA_ONLY),
        workspace=workspace,
        runner=SubprocessRunner(),
    )


@pytest.mark.parametrize(
    ("chart", "status", "findings"),
    [
        ("passing-app", "passed", ()),
        ("schema-violator", "failed", ("Deployment/schema-violator", "/spec/replicas")),
    ],
)
def test_fixture_charts_render_and_meet_or_fail_their_schemas(
    tmp_path: Path,
    schema_workspace: RepositoryWorkspace,
    chart: str,
    status: str,
    findings: tuple[str, ...],
) -> None:
    require("helm", "kubeconform", "git")
    shutil.copytree(FIXTURES / "schemas", tmp_path / "schemas")
    fixture_chart(tmp_path, chart, schemaLocations=[SCHEMA_LOCATION])

    outcome = check(schema_workspace, chart)

    (row,) = outcome.rows
    assert row.checks["render"].status == "passed", row.checks["render"].detail
    assert row.checks["schema"].status == status, row.checks["schema"].detail
    assert all(finding in row.checks["schema"].detail for finding in findings)


def test_a_kind_with_no_schema_anywhere_is_an_error_with_remediation(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    require("helm", "kubeconform", "git")
    chart = write_validation_chart(tmp_path, "gadgets")
    (chart / "templates").mkdir()
    (chart / "templates/gadget.yaml").write_text(
        "apiVersion: example.io/v1\nkind: Gadget\nmetadata: {name: demo}\n"
    )

    (row,) = check(schema_workspace, "gadgets").rows

    assert row.checks["schema"].status == "error"
    assert "could not find schema" in row.checks["schema"].detail
    assert "chart-local schema" in row.checks["schema"].detail


def test_new_kinds_and_changed_crds_validate_without_sync_or_lock_changes(
    tmp_path: Path, schema_workspace: RepositoryWorkspace, caplog: pytest.LogCaptureFixture
) -> None:
    require("helm", "kubeconform", "git")
    lock_path = tmp_path / ".chart-manager/schemas.lock.yaml"
    before = lock_path.read_bytes()
    provider = write_validation_chart(tmp_path, "provider")
    consumer = write_validation_chart(tmp_path, "consumer")
    unrelated = write_validation_chart(tmp_path, "unrelated")
    for chart, manifest in (
        (provider, crd_manifest()),
        (
            consumer,
            "apiVersion: example.io/v1\nkind: Widget\nmetadata: {name: demo}\nspec: {name: hello}\n",
        ),
        (
            unrelated,
            dump_yaml({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x"}}),
        ),
    ):
        (chart / "templates").mkdir()
        (chart / "templates/resources.yaml").write_text(manifest)
    (unrelated / "values.yaml").write_text("[broken YAML")

    def validated() -> validate.ValidateOutcome:
        outcome = check(schema_workspace, "consumer")
        assert not outcome.spec_errors, outcome.spec_errors
        assert lock_path.read_bytes() == before
        return outcome

    with caplog.at_level(logging.INFO):
        assert validated().outcome() is Outcome.SUCCESS
    assert "Preparing CRD schemas: 0 providers cached, 1 to render" in caplog.messages
    # A newly introduced built-in kind comes from the full pinned snapshot.
    (consumer / "templates/new.yaml").write_text(
        "apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata: {name: demo}\n"
    )
    assert validated().outcome() is Outcome.SUCCESS
    # Current generated CRDs override the permissive catalog schema immediately.
    (provider / "templates/resources.yaml").write_text(crd_manifest(nested_type="integer"))
    outcome = validated()
    assert outcome.outcome() is Outcome.FAILED
    assert "want integer" in outcome.rows[0].checks["schema"].detail
    (consumer / "templates/resources.yaml").write_text(
        "apiVersion: example.io/v1\nkind: Widget\nmetadata: {name: demo}\nspec: {name: 12}\n"
    )
    assert validated().outcome() is Outcome.SUCCESS
