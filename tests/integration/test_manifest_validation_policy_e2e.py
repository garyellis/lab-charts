"""`validate.run()` with real helm, kubeconform and kyverno against the repository policies."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from chart_manager.commands import validate
from chart_manager.plumbing.commands import SubprocessRunner
from chart_manager.shared.workspace import RepositoryWorkspace
from tests.integration.conftest import FIXTURES, fixture_chart, require

pytestmark = pytest.mark.integration

REPO_POLICIES = Path(__file__).parent.parent.parent / "policies"
SCHEMA_LOCATION = "schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"


@pytest.mark.parametrize(
    ("chart", "policy", "findings"),
    [
        ("passing-app", "passed", ()),
        (
            "policy-violator",
            "failed",
            (
                "require-non-root",
                "Deployment/policy-violator",
                "forbid-load-balancer",
                "Service/policy-violator",
            ),
        ),
    ],
)
def test_fixture_charts_meet_or_break_the_repository_policies(
    tmp_path: Path,
    schema_workspace: RepositoryWorkspace,
    chart: str,
    policy: str,
    findings: tuple[str, ...],
) -> None:
    require("helm", "kubeconform", "kyverno", "git")
    shutil.copytree(FIXTURES / "schemas", tmp_path / "schemas")
    shutil.copytree(REPO_POLICIES, tmp_path / "policies")
    fixture_chart(tmp_path, chart, schemaLocations=[SCHEMA_LOCATION])

    outcome = validate.run(
        validate.ValidateRequest(out=tmp_path / "out", charts=(chart,)),
        workspace=schema_workspace,
        runner=SubprocessRunner(),
    )

    (row,) = outcome.rows
    assert row.checks["schema"].status == "passed", row.checks["schema"].detail
    assert row.checks["policy"].status == policy, row.checks["policy"].detail
    # Both repository policies fire on the violator, keeping it honest for the whole directory.
    assert all(finding in row.checks["policy"].detail for finding in findings)
