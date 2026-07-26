"""Pure unit tests for manifest-validation catalog discovery."""

from __future__ import annotations

from pathlib import Path

from chart_manager.services.manifest_validation.catalog import (
    discover_chart_manager_config,
)
from chart_manager.services.manifest_validation.compiler import discover_policies


def test_discover_policies_returns_both_dirs_when_present(tmp_path: Path) -> None:
    (tmp_path / "policies").mkdir()
    (tmp_path / "charts" / "alpha" / "policies").mkdir(parents=True)

    result = discover_policies(tmp_path, tmp_path / "charts" / "alpha")

    assert result == [tmp_path / "policies", tmp_path / "charts" / "alpha" / "policies"]


def test_discover_policies_only_repo_dir_present(tmp_path: Path) -> None:
    (tmp_path / "policies").mkdir()
    # chart dir exists but no policies/ subdir
    (tmp_path / "charts" / "alpha").mkdir(parents=True)

    result = discover_policies(tmp_path, tmp_path / "charts" / "alpha")

    assert result == [tmp_path / "policies"]


def test_discover_policies_only_chart_dir_present(tmp_path: Path) -> None:
    (tmp_path / "charts" / "alpha" / "policies").mkdir(parents=True)

    result = discover_policies(tmp_path, tmp_path / "charts" / "alpha")

    assert result == [tmp_path / "charts" / "alpha" / "policies"]


def test_discover_policies_neither_present(tmp_path: Path) -> None:
    result = discover_policies(tmp_path, tmp_path / "charts" / "alpha")
    assert result == []


def test_discover_policies_ignores_files_named_policies(tmp_path: Path) -> None:
    # `is_dir()` filter — a stray file at <root>/policies should NOT
    # be returned as a discovered directory.
    (tmp_path / "policies").write_text("not a dir")

    result = discover_policies(tmp_path, tmp_path / "charts" / "alpha")

    assert result == []


def test_discover_chart_manager_config_present(tmp_path: Path) -> None:
    chart_dir = tmp_path / "charts" / "alpha"
    chart_dir.mkdir(parents=True)
    spec = chart_dir / "chart-manager.yaml"
    spec.write_text("version: 1\nenabled: true\n")

    result = discover_chart_manager_config(chart_dir)

    assert result == spec


def test_discover_chart_manager_config_absent(tmp_path: Path) -> None:
    (tmp_path / "charts" / "alpha").mkdir(parents=True)

    result = discover_chart_manager_config(tmp_path / "charts" / "alpha")

    assert result is None


def test_discovery_does_not_accept_the_legacy_filename(tmp_path: Path) -> None:
    chart_dir = tmp_path / "charts" / "alpha"
    chart_dir.mkdir(parents=True)
    (chart_dir / "validate-spec.yaml").write_text("version: 1\n")

    assert discover_chart_manager_config(chart_dir) is None


def test_discover_chart_manager_config_is_dir_returns_none(tmp_path: Path) -> None:
    # Defensive: if someone created chart-manager.yaml as a directory by
    # accident, the helper must not return it (is_file() filter).
    spec_path = tmp_path / "charts" / "alpha" / "chart-manager.yaml"
    spec_path.mkdir(parents=True)

    result = discover_chart_manager_config(tmp_path / "charts" / "alpha")

    assert result is None
