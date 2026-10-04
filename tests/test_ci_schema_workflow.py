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
                 "test_chart_lifecycle_packaging.py", "test_schema_precedence.py"):
        assert name in script


@pytest.mark.parametrize("mode,changes,needed", [
    ("diff", "README.md\ndocs/architecture.md", False),
    ("diff", "tests/test_something.py\nrenovate.json", False),
    ("diff", "charts/demo/values.yaml", True),
    ("diff", ".chart-manager/workspace.yaml", True),
    ("diff", "src/chart_manager/commands/validate/schemas/crd.py", True),
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


def _cache_helper():
    import runpy

    return runpy.run_path(str(ROOT / ".github/scripts/validation_cache.py"))


def test_cache_restore_never_replaces_checkout_files(tmp_path: Path) -> None:
    helper = _cache_helper()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    chart = tmp_path / "charts" / "demo"
    artifacts = chart / "charts"
    nested = artifacts / "expanded" / "templates"
    nested.mkdir(parents=True)
    (chart / "Chart.yaml").write_text("name: demo\nversion: 1.0.0\n")
    tracked = artifacts / "tracked.tgz"
    tracked.write_bytes(b"checkout-owned")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    downloaded = artifacts / "downloaded.tgz"
    downloaded.write_bytes(b"fetched dependency")
    (nested / "resource.yaml").write_text("kind: ConfigMap\n")
    git_metadata = artifacts / "expanded" / ".git"
    git_metadata.mkdir()
    (git_metadata / "config").write_text("private clone configuration")
    link = artifacts / "linked.tgz"
    link.symlink_to(downloaded)
    cache = tmp_path / ".cache" / "helm"
    helper["dependencies"](tmp_path, cache, restore=False)
    staged = cache / "demo" / "files"
    assert not (staged / "tracked.tgz").exists()
    assert not (staged / "linked.tgz").exists()
    assert not (staged / "expanded" / ".git").exists()
    # Even if a restored cache carries an old copy, checkout bytes always win.
    (staged / "tracked.tgz").write_bytes(b"stale")
    downloaded.unlink()
    (nested / "resource.yaml").unlink()
    helper["dependencies"](tmp_path, cache, restore=True)
    assert downloaded.read_bytes() == b"fetched dependency"
    assert (nested / "resource.yaml").read_text() == "kind: ConfigMap\n"
    assert tracked.read_bytes() == b"checkout-owned"
    downloaded.write_bytes(b"already present")
    helper["dependencies"](tmp_path, cache, restore=True)
    assert downloaded.read_bytes() == b"already present"
    downloaded.unlink()
    (chart / "Chart.yaml").write_text("name: demo\nversion: 2.0.0\n")
    helper["dependencies"](tmp_path, cache, restore=True)
    assert not downloaded.exists()


def test_derived_cache_save_contains_only_current_chart_results(tmp_path: Path) -> None:
    import json

    helper = _cache_helper()
    charts = tmp_path / "charts"
    charts.mkdir()
    current = "a" * 64 + ".json"
    stale = "b" * 64 + ".json"
    (charts / current).write_text("current")
    (charts / stale).write_text("stale")
    (tmp_path / "current-charts.json").write_text(json.dumps([current]))
    helper["prune_derived"](tmp_path)
    assert sorted(path.name for path in charts.iterdir()) == [current]
    (tmp_path / "current-charts.json").unlink()
    helper["prune_derived"](tmp_path)
    assert not list(charts.iterdir())


def test_cache_keys_and_save_conditions_preserve_successful_preparation() -> None:
    derived = _step("Restore derived CRD schemas")
    assert "github.sha" not in derived["with"]["key"]
    assert "hashFiles" in derived["with"]["key"]
    assert derived["with"]["path"].endswith("/derived/charts")
    upstream = _step("Save locked schema generation")
    assert upstream["uses"] == "actions/cache/save@v5"
    assert "steps.schemas.outcome == 'success'" in upstream["if"]
    assert "validate.outcome" not in upstream["if"]
    for name in ("Save Helm dependencies", "Save derived CRD schemas"):
        assert "steps.validate.outcome == 'success'" in _step(name)["if"]
        assert "cache-primary-key" in _step(name)["with"]["key"]
