"""CLI contract tests for ``chart-manager plan``'s explain projection.

Ported from the removed ``chart-manager lifecycle`` group. The lifecycle
group exposed compiler and evidence internals as a product surface; only the
impact explainer carried capability that exists nowhere else, since
``plan -o github`` computes the same selection and projects the reasons away
(see ``tests/test_ci_matrix_cli.py``).

``plan`` defaults to ``-o table``, which is this projection, so the bare
``plan`` invocations below are the explain view.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from chart_manager.cli import main as main_cli

from .conftest import cli


def _impact_result(
    *,
    spec_errors: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> Any:
    reason = SimpleNamespace(
        code="validation-trigger",
        changed_file=Path("charts/grafana/values-dev.yaml"),
        detail="authored validation trigger selected grafana",
    )
    validation = SimpleNamespace(
        chart="grafana",
        environment="dev",
        reasons=(reason,),
    )
    cluster = SimpleNamespace(
        chart="grafana",
        profile="minimal",
        reasons=(
            SimpleNamespace(
                code="chart-change",
                changed_file=Path("charts/grafana/values-dev.yaml"),
                detail="changed file belongs to grafana",
            ),
        ),
    )
    payload = {
        "apiVersion": "lifecycle.cmg.io/v1alpha1",
        "kind": "LifecycleImpact",
        "changedFiles": [
            "charts/grafana/values-dev.yaml",
            "kind-config.yaml",
        ],
        "validationSelection": [
            {
                "chart": "grafana",
                "environment": "dev",
                "reasons": [
                    {
                        "code": "validation-trigger",
                        "changedFile": "charts/grafana/values-dev.yaml",
                        "detail": "authored validation trigger selected grafana",
                    }
                ],
            }
        ],
        "clusterTestMatrix": [
            {
                "chart": "grafana",
                "profile": "minimal",
                "reasons": [
                    {
                        "code": "chart-change",
                        "changedFile": "charts/grafana/values-dev.yaml",
                        "detail": "changed file belongs to grafana",
                    }
                ],
            }
        ],
        "specErrors": list(spec_errors),
        "warnings": list(warnings),
    }
    return SimpleNamespace(
        validation=(validation,),
        cluster_tests=(cluster,),
        spec_errors=spec_errors,
        warnings=warnings,
        to_dict=lambda: payload,
    )


def test_root_help_no_longer_exposes_the_lifecycle_group() -> None:
    """The lifecycle group is gone and its one surviving command is now `plan`.

    Mirrors the `deps` removal guard in test_cluster_tests.py: a removed
    group that silently comes back is the regression this pins.

    Asserted by invoking the group rather than string-matching the root
    help -- "lifecycle" legitimately appears in the `events` and `charts`
    descriptions, so a substring check would pass for the wrong reason.

    P1.6 moved the impact explainer from `ci impact` to `plan -o table`, so
    the surviving capability is now looked for under `plan`. `ci impact`
    still runs as a hidden alias, which is exactly why it is no longer
    listed in `ci --help` and cannot be asserted for here --
    `tests/test_cli_aliases.py` owns that spelling now.
    """
    removed = cli("lifecycle", "--help")
    root_help = cli("--help")

    assert removed.exit_code == 2
    assert root_help.exit_code == 0
    assert "plan" in root_help.stdout


def test_impact_combines_changed_file_sources_and_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    changed_files = tmp_path / "changes.txt"
    changed_files.write_text(
        "\ncharts/grafana/values-dev.yaml\n\n",
        encoding="utf-8",
    )
    captured: list[list[str]] = []
    result_object = _impact_result()
    monkeypatch.setattr(
        main_cli,
        "_impact_service",
        lambda root: SimpleNamespace(
            analyze=lambda changes: captured.append(changes) or result_object
        ),
    )

    result = cli(
        "plan",
        "--changed-files",
        str(changed_files),
        "--changed-file",
        "kind-config.yaml",
        "-o",
        "json",
        "--root",
        str(tmp_path),
    )

    assert result.exit_code == 0
    assert captured == [["charts/grafana/values-dev.yaml", "kind-config.yaml"]]
    assert json.loads(result.stdout)["kind"] == "LifecycleImpact"


def test_impact_requires_an_explicit_change_source() -> None:
    result = cli("plan")

    assert result.exit_code == 2
    assert "--changed-files / --changed-file" in result.stderr


def test_impact_changed_files_read_error_is_bad_parameter(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"

    result = cli("plan", "--changed-files", str(missing))

    assert result.exit_code == 2
    assert "--changed-files" in result.stderr
    assert "cannot read changed-files input" in result.stderr


def test_impact_rejects_an_unknown_format_before_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unusable projection must fail at parse time, not after the work.

    P1.6 renamed this command's `--format text|json|yaml` to `plan`'s local
    `--output/-o table|json|yaml|github`; `text` became `table`. The
    property under test is unchanged: the value is validated by an option
    callback, so it fires before the service is ever constructed.
    """
    monkeypatch.setattr(
        main_cli,
        "_impact_service",
        lambda root: pytest.fail("service should not be constructed"),
    )

    result = cli("plan", "--changed-file", "kind-config.yaml", "-o", "toml")

    assert result.exit_code == 2
    assert "--output" in result.stderr
    assert "table, json, yaml, github" in result.stderr


def test_impact_text_shows_reasons_warnings_and_exits_on_spec_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_object = _impact_result(
        spec_errors=("grafana: unknown environment prod",),
        warnings=("unmatched chart change README.md",),
    )
    monkeypatch.setattr(
        main_cli,
        "_impact_service",
        lambda root: SimpleNamespace(analyze=lambda changes: result_object),
    )

    result = cli("plan", "--changed-file", "charts/grafana/values-dev.yaml")

    assert result.exit_code == 1
    assert "Validation:" in result.stdout
    assert "grafana/dev" in result.stdout
    assert "validation-trigger: charts/grafana/values-dev.yaml" in result.stdout
    assert "Cluster tests:" in result.stdout
    assert "grafana/minimal" in result.stdout
    assert "Warnings:" in result.stdout
    assert "unmatched chart change README.md" in result.stdout
    assert "Spec errors:" in result.stdout
    assert "grafana: unknown environment prod" in result.stdout


def test_impact_yaml_preserves_machine_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_object = _impact_result()
    monkeypatch.setattr(
        main_cli,
        "_impact_service",
        lambda root: SimpleNamespace(analyze=lambda changes: result_object),
    )

    result = cli(
        "plan",
        "--changed-file",
        "charts/grafana/values-dev.yaml",
        "-o",
        "yaml",
    )

    assert result.exit_code == 0
    payload = yaml.safe_load(result.stdout)
    assert payload["apiVersion"] == "lifecycle.cmg.io/v1alpha1"
    assert payload["kind"] == "LifecycleImpact"
    assert payload["clusterTestMatrix"][0]["profile"] == "minimal"
