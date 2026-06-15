"""End-to-end render -> schema -> policy integration test.

Skips cleanly if helm, kubeconform, or kyverno are missing on PATH so
unit-test runs on contributor machines without the validate tooling stay
green. CI install of these tools is handled by `chart-manager validate
deps-install` (kyverno lands in M6).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.integrations.kyverno import Kyverno
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.validate_models import WorklistRow
from chart_manager.services.validate.runner import RowConfig, ValidateRunner

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).parent.parent.parent
FIXTURE_CHARTS = Path(__file__).parent.parent / "fixtures" / "charts"
REPO_POLICIES = REPO_ROOT / "policies"


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
        kyverno=Kyverno(runner=cmd_runner),
    )


def _cfg(chart_dir: Path, *, env: str = "dev") -> RowConfig:
    row = WorklistRow(
        chart=chart_dir.name,
        env=env,
        release=chart_dir.name,
        namespace=f"lab-{env}",
    )
    values = [chart_dir / "values.yaml"] if (chart_dir / "values.yaml").is_file() else []
    return RowConfig(
        row=row,
        chart_path=chart_dir,
        values=values,
        policy_paths=[REPO_POLICIES],
    )


def test_passing_app_renders_schema_passes_policy_passes(tmp_path: Path) -> None:
    _skip_if_missing("helm", "kubeconform", "kyverno")
    chart = FIXTURE_CHARTS / "passing-app"

    result = _runner(tmp_path / "out").run([_cfg(chart)])

    row = result.rows[0]
    assert row.phases["render"].status == "PASS", row.phases["render"].detail
    assert row.phases["schema"].status == "PASS", row.phases["schema"].detail
    assert row.phases["policy"].status == "PASS", row.phases["policy"].detail
    assert result.exit_code() == 0


def test_policy_violator_passes_schema_fails_policy(tmp_path: Path) -> None:
    _skip_if_missing("helm", "kubeconform", "kyverno")
    chart = FIXTURE_CHARTS / "policy-violator"

    result = _runner(tmp_path / "out").run([_cfg(chart)])

    row = result.rows[0]
    assert row.phases["render"].status == "PASS", row.phases["render"].detail
    assert row.phases["schema"].status == "PASS", row.phases["schema"].detail
    assert row.phases["policy"].status == "FAIL"
    detail = row.phases["policy"].detail or ""
    # Both authored policies should fire on this fixture:
    #   - require-non-root: Deployment lacks runAsNonRoot
    #   - forbid-load-balancer: Service is type LoadBalancer
    # Asserting both keeps the fixture honest as a known-violator for the
    # whole policy/ directory, not just one rule.
    assert "require-non-root" in detail
    assert "Deployment/policy-violator" in detail
    assert "forbid-load-balancer" in detail
    assert "Service/policy-violator" in detail
    assert result.exit_code() == 1
