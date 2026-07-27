"""Focused tests for authored-to-runtime manifest-validation compilation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from chart_manager.plumbing.errors import SpecError
from chart_manager.services.manifest_validation.catalog import load_manifest_validation_target
from chart_manager.services.manifest_validation.compiler import resolve_manifest_validation
from chart_manager.services.manifest_validation.models import ManifestValidationTarget


def _target(
    root: Path,
    *,
    values: str = "values.yaml",
    extra: str = "",
) -> ManifestValidationTarget:
    chart = root / "charts" / "alpha"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: alpha\nversion: 0.1.0\n")
    (chart / "chart-lifecycle.yaml").write_text(
        "apiVersion: lifecycle.cmg.io/v1alpha1\n"
        "kind: ChartLifecycle\n"
        "metadata:\n"
        "  name: alpha\n"
        "spec:\n"
        "  validation:\n"
        "    releaseName: alpha\n"
        "    environments:\n"
        "      dev:\n"
        "        namespace: lab-dev\n"
        f"        values: [{values}]\n"
        f"{textwrap.indent(extra, '    ') if extra else ''}"
    )
    return load_manifest_validation_target(root, "alpha")


def test_missing_required_value_fails_with_environment_and_authored_path(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)

    with pytest.raises(SpecError) as caught:
        resolve_manifest_validation(target, tmp_path)

    message = str(caught.value)
    assert "environment 'dev'" in message
    assert "value file 'values.yaml'" in message
    assert str(target.spec_path) in message
    assert "does not exist" in message


def test_required_value_must_be_a_regular_file(tmp_path: Path) -> None:
    target = _target(tmp_path)
    (target.path / "values.yaml").mkdir()

    with pytest.raises(SpecError, match="is not a regular file"):
        resolve_manifest_validation(target, tmp_path)


def test_required_value_must_resolve_beneath_chart(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    outside = tmp_path / "outside.yaml"
    outside.write_text("{}\n")
    (target.path / "values.yaml").symlink_to(outside)

    with pytest.raises(SpecError, match="escapes its base directory"):
        resolve_manifest_validation(target, tmp_path)


def test_missing_extra_policy_is_omitted_with_warning(tmp_path: Path) -> None:
    target = _target(
        tmp_path,
        extra="policies:\n  extra: [missing-policies]\n",
    )
    (target.path / "values.yaml").write_text("{}\n")

    compiled = resolve_manifest_validation(target, tmp_path)

    assert compiled.policy_paths == ()
    assert len(compiled.warnings) == 1
    assert "policy directory does not exist" in compiled.warnings[0]
    assert str(target.spec_path) in compiled.warnings[0]


def test_extra_policy_must_be_a_directory(tmp_path: Path) -> None:
    target = _target(
        tmp_path,
        extra="policies:\n  extra: [policy.yaml]\n",
    )
    (target.path / "values.yaml").write_text("{}\n")
    (target.path / "policy.yaml").write_text("apiVersion: kyverno.io/v1\n")

    compiled = resolve_manifest_validation(target, tmp_path)

    assert compiled.policy_paths == ()
    assert len(compiled.warnings) == 1
    assert "policy path is not a directory" in compiled.warnings[0]


def test_missing_local_schema_location_fails_early(tmp_path: Path) -> None:
    target = _target(
        tmp_path,
        extra="schemaLocations: [schemas/custom.json]\n",
    )
    (target.path / "values.yaml").write_text("{}\n")

    with pytest.raises(SpecError) as caught:
        resolve_manifest_validation(target, tmp_path)

    message = str(caught.value)
    assert "local schema location 'schemas/custom.json'" in message
    assert str(target.spec_path) in message
    assert "does not exist" in message


def test_local_schema_template_requires_existing_base_directory(
    tmp_path: Path,
) -> None:
    target = _target(
        tmp_path,
        extra='schemaLocations: ["schemas/{{.ResourceKind}}.json"]\n',
    )
    (target.path / "values.yaml").write_text("{}\n")

    with pytest.raises(SpecError, match="missing template base directory"):
        resolve_manifest_validation(target, tmp_path)


def test_schema_locations_preserve_keywords_and_urls_and_absolutize_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(
        tmp_path,
        extra=(
            "schemaLocations:\n"
            "  - default\n"
            "  - https://schemas.example.test/{{.ResourceKind}}.json\n"
            '  - "schemas/{{.ResourceKind}}.json"\n'
        ),
    )
    (target.path / "values.yaml").write_text("{}\n")
    (tmp_path / "schemas").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    compiled = resolve_manifest_validation(target, tmp_path)

    assert compiled.schema_locations == (
        "default",
        "https://schemas.example.test/{{.ResourceKind}}.json",
        str((tmp_path / "schemas" / "{{.ResourceKind}}.json").resolve()),
    )


def test_local_schema_location_must_resolve_beneath_repository(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    target = _target(
        root,
        extra='schemaLocations: ["schemas/{{.ResourceKind}}.json"]\n',
    )
    (target.path / "values.yaml").write_text("{}\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "schemas").symlink_to(outside)

    with pytest.raises(SpecError, match="escapes its base directory"):
        resolve_manifest_validation(target, root)
