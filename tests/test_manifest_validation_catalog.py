"""Pure unit tests for manifest-validation policy-path discovery."""

from __future__ import annotations

from pathlib import Path

from chart_manager.services.manifest_validation.validator_adapters import (
    discover_policy_paths,
)


def test_discover_policies_returns_both_dirs_when_present(tmp_path: Path) -> None:
    (tmp_path / "policies").mkdir()
    (tmp_path / "charts" / "alpha" / "policies").mkdir(parents=True)

    result = discover_policy_paths(tmp_path, tmp_path / "charts" / "alpha")

    assert result == (tmp_path / "policies", tmp_path / "charts" / "alpha" / "policies")


def test_discover_policies_only_repo_dir_present(tmp_path: Path) -> None:
    (tmp_path / "policies").mkdir()
    # chart dir exists but no policies/ subdir
    (tmp_path / "charts" / "alpha").mkdir(parents=True)

    result = discover_policy_paths(tmp_path, tmp_path / "charts" / "alpha")

    assert result == (tmp_path / "policies",)


def test_discover_policies_only_chart_dir_present(tmp_path: Path) -> None:
    (tmp_path / "charts" / "alpha" / "policies").mkdir(parents=True)

    result = discover_policy_paths(tmp_path, tmp_path / "charts" / "alpha")

    assert result == (tmp_path / "charts" / "alpha" / "policies",)


def test_discover_policies_neither_present(tmp_path: Path) -> None:
    result = discover_policy_paths(tmp_path, tmp_path / "charts" / "alpha")
    assert result == ()


def test_discover_policies_ignores_files_named_policies(tmp_path: Path) -> None:
    # `is_dir()` filter — a stray file at <root>/policies should NOT
    # be returned as a discovered directory.
    (tmp_path / "policies").write_text("not a dir")

    result = discover_policy_paths(tmp_path, tmp_path / "charts" / "alpha")

    assert result == ()
