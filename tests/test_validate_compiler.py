"""Focused tests for authored-to-runtime validation configuration compilation."""
from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.plumbing.errors import SpecError
from chart_manager.services.validate.catalog import load_validatable_chart
from chart_manager.services.validate.compiler import compile_validate_spec
from chart_manager.services.validate.domain.models import ValidatableChart


def _target(
    root: Path,
    *,
    values: str = "values.yaml",
    extra: str = "",
) -> ValidatableChart:
    chart = root / "charts" / "alpha"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: alpha\nversion: 0.1.0\n"
    )
    (chart / "validate-spec.yaml").write_text(
        "version: 1\n"
        "release_name: alpha\n"
        "environments:\n"
        "  dev:\n"
        "    namespace: lab-dev\n"
        f"    values: [{values}]\n"
        f"{extra}"
    )
    return load_validatable_chart(root, "alpha")


def test_missing_required_value_fails_with_environment_and_authored_path(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)

    with pytest.raises(SpecError) as caught:
        compile_validate_spec(target, tmp_path)

    message = str(caught.value)
    assert "environment 'dev'" in message
    assert "value file 'values.yaml'" in message
    assert str(target.spec_path) in message
    assert "does not exist" in message


def test_required_value_must_be_a_regular_file(tmp_path: Path) -> None:
    target = _target(tmp_path)
    (target.path / "values.yaml").mkdir()

    with pytest.raises(SpecError, match="is not a regular file"):
        compile_validate_spec(target, tmp_path)


def test_required_value_must_resolve_beneath_chart(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    outside = tmp_path / "outside.yaml"
    outside.write_text("{}\n")
    (target.path / "values.yaml").symlink_to(outside)

    with pytest.raises(SpecError, match="escapes its base directory"):
        compile_validate_spec(target, tmp_path)


def test_missing_extra_policy_is_omitted_with_warning(tmp_path: Path) -> None:
    target = _target(
        tmp_path,
        extra="policies:\n  extra: [missing-policies]\n",
    )
    (target.path / "values.yaml").write_text("{}\n")

    compiled = compile_validate_spec(target, tmp_path)

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

    compiled = compile_validate_spec(target, tmp_path)

    assert compiled.policy_paths == ()
    assert len(compiled.warnings) == 1
    assert "policy path is not a directory" in compiled.warnings[0]


def test_missing_local_schema_location_fails_early(tmp_path: Path) -> None:
    target = _target(
        tmp_path,
        extra="schema_locations: [schemas/custom.json]\n",
    )
    (target.path / "values.yaml").write_text("{}\n")

    with pytest.raises(SpecError) as caught:
        compile_validate_spec(target, tmp_path)

    message = str(caught.value)
    assert "local schema location 'schemas/custom.json'" in message
    assert str(target.spec_path) in message
    assert "does not exist" in message


def test_local_schema_template_requires_existing_base_directory(
    tmp_path: Path,
) -> None:
    target = _target(
        tmp_path,
        extra='schema_locations: ["schemas/{{.ResourceKind}}.json"]\n',
    )
    (target.path / "values.yaml").write_text("{}\n")

    with pytest.raises(SpecError, match="missing template base directory"):
        compile_validate_spec(target, tmp_path)


def test_schema_locations_preserve_keywords_and_urls_and_absolutize_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(
        tmp_path,
        extra=(
            "schema_locations:\n"
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

    compiled = compile_validate_spec(target, tmp_path)

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
        extra='schema_locations: ["schemas/{{.ResourceKind}}.json"]\n',
    )
    (target.path / "values.yaml").write_text("{}\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "schemas").symlink_to(outside)

    with pytest.raises(SpecError, match="escapes its base directory"):
        compile_validate_spec(target, root)
