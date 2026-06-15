"""End-to-end render -> schema integration test.

Skips cleanly if helm or kubeconform are not on PATH so unit-test runs on
contributor machines without the validate tooling stay green. CI install
of these tools is handled by `chart-manager validate deps-install` (M1b
ships helm; kubeconform/conftest land in M6).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.validate_models import WorklistRow
from chart_manager.services.validate.runner import RowConfig, ValidateRunner

pytestmark = pytest.mark.integration

FIXTURE_CHARTS = Path(__file__).parent.parent / "fixtures" / "charts"


def _skip_if_missing(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        pytest.skip(f"missing tools on PATH: {', '.join(missing)}")


def _runner(out_root: Path) -> ValidateRunner:
    cmd_runner = CommandRunner()
    return ValidateRunner(
        helm=Helm(runner=cmd_runner),
        output_root=out_root,
        kubeconform=Kubeconform(runner=cmd_runner),
    )


def _inputs(chart_dir: Path, *, env: str = "dev") -> RowConfig:
    row = WorklistRow(
        chart=chart_dir.name,
        env=env,
        release=chart_dir.name,
        namespace=f"lab-{env}",
    )
    values = [chart_dir / "values.yaml"] if (chart_dir / "values.yaml").is_file() else []
    return RowConfig(row=row, chart_path=chart_dir, values=values)


def test_passing_app_renders_and_passes_schema(tmp_path: Path) -> None:
    _skip_if_missing("helm", "kubeconform")
    chart = FIXTURE_CHARTS / "passing-app"

    result = _runner(tmp_path / "out").run([_inputs(chart)])

    row = result.rows[0]
    assert row.phases["render"].status == "PASS"
    assert row.phases["schema"].status == "PASS", row.phases["schema"].detail
    assert result.exit_code() == 0


def test_schema_violator_renders_and_fails_schema(tmp_path: Path) -> None:
    _skip_if_missing("helm", "kubeconform")
    chart = FIXTURE_CHARTS / "schema-violator"

    result = _runner(tmp_path / "out").run([_inputs(chart)])

    row = result.rows[0]
    assert row.phases["render"].status == "PASS"
    assert row.phases["schema"].status == "FAIL"
    detail = row.phases["schema"].detail or ""
    assert "Deployment/schema-violator" in detail
    assert "/spec/replicas" in detail
    assert result.exit_code() == 1
