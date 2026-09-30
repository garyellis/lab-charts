"""Execute schema CI shell steps to pin scope and failure-diagnostic behavior."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from chart_manager.plumbing.yaml_files import load_yaml_file

ROOT = Path(__file__).resolve().parents[1]


def _step(name: str) -> dict:
    workflow = load_yaml_file(ROOT / ".github/workflows/ci.yaml")
    return next(step for step in workflow["jobs"]["validate"]["steps"] if step.get("name") == name)


def test_real_schema_contracts_are_required_by_ci() -> None:
    workflow = load_yaml_file(ROOT / ".github/workflows/ci.yaml")
    step = next(step for step in workflow["jobs"]["layering"]["steps"]
                if step.get("name") == "ChartLifecycle and schema integration contracts")
    script = step["run"]
    assert script.index("kubeconform -v") < script.index("pytest")
    assert script.index("helm version") < script.index("pytest")
    for name in ("test_crd_schema_constraints.py", "test_manifest_validation_schema_e2e.py",
                 "test_chart_lifecycle_packaging.py"):
        assert name in script


@pytest.mark.parametrize("mode,changes,needed", [
    ("diff", "README.md\ndocs/architecture.md", False),
    ("diff", "tests/test_something.py\nrenovate.json", False),
    ("diff", "charts/demo/values.yaml", True),
    ("diff", ".chart-manager/workspace.yaml", True),
    ("diff", "src/chart_manager/services/kubeconform_schemas/crd.py", True),
    ("diff", ".mise.toml", True),
    ("diff", ".github/workflows/ci.yaml", True),
    ("diff", "custom-schema/widget.json", True),
    ("all", "", True), ("list", "", True),
    ("diff", "", True), ("diff", "\n ", True),
])
def test_validation_scope(tmp_path: Path, mode: str, changes: str, needed: bool) -> None:
    output = tmp_path / "output"
    result = subprocess.run(["bash", "-e", "-c", _step("Decide validation scope")["run"]],
        env={**os.environ, "MODE": mode, "CHANGED_FILES": changes,
             "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary")},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text().strip() == f"needed={str(needed).lower()}"


def test_schema_failure_keeps_exit_status_and_actual_diagnostic(tmp_path: Path) -> None:
    script = "mise() { echo 'demo/dev: broken template' >&2; return 4; };\n"
    script += _step("Synchronize locked schemas")["run"]
    summary = tmp_path / "summary"
    result = subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path,
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary)}, capture_output=True, text=True)
    assert result.returncode == 4
    assert "demo/dev: broken template" in summary.read_text()
    assert "--refresh" not in summary.read_text()


def test_failed_schema_preparation_runs_repository_render_diagnostics(tmp_path: Path) -> None:
    script = "mise() { printf '%s\\n' \"$@\"; };\n" + _step("Validate")["run"]
    result = subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path,
        env={**os.environ, "MODE": "diff", "CHANGED_FILES": "charts/demo/values.yaml",
             "SCHEMAS_OUTCOME": "failure"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert args[:6] == ["run", "validate", "--", "--all", "--phase", "render"]
    assert "--keep" in args
    assert "--github-step-summary" in args
