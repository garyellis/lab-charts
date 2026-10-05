"""Containment-safe chart path resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from chart_manager.commands.upgrade.models import UpgradeError
from chart_manager.plumbing.errors import YamlError
from chart_manager.plumbing.yaml_files import load_yaml_file


def _reject_symlinks(path: Path, stop: Path) -> None:
    current = path
    while current != stop:
        if current.is_symlink():
            raise UpgradeError(f"upgrade path must not contain symlinks: {current}")
        parent = current.parent
        if parent == current:
            break
        current = parent


def resolve_chart_path(
    root: Path,
    chart_path: Path,
    *,
    charts_dir: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    """Resolve and validate one chart without allowing an escape from ``root``."""
    try:
        repo_root = root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise UpgradeError(f"repository root does not exist: {root}") from exc
    raw = chart_path.expanduser()
    if raw.is_absolute():
        candidate = raw
    elif len(raw.parts) == 1:
        candidate = repo_root / charts_dir / raw.name
    else:
        candidate = repo_root / raw
    _reject_symlinks(candidate, repo_root)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(repo_root)
    except (OSError, ValueError) as exc:
        raise UpgradeError(f"chart path must resolve inside repository root: {chart_path}") from exc
    if not resolved.is_dir():
        raise UpgradeError(f"chart path is not a directory: {resolved}")
    chart_file = resolved / "Chart.yaml"
    if chart_file.is_symlink():
        raise UpgradeError(f"Chart.yaml must not be a symlink: {chart_file}")
    if not chart_file.is_file():
        raise UpgradeError(f"missing Chart.yaml: {chart_file}")
    try:
        document = load_yaml_file(chart_file)
    except YamlError as exc:
        raise UpgradeError(f"invalid Chart.yaml {chart_file}: {exc}") from exc
    name = document.get("name")
    if not isinstance(name, str) or name != resolved.name:
        raise UpgradeError(
            f"Chart.yaml name {name!r} does not match chart directory {resolved.name!r}"
        )
    return repo_root, resolved, document


def safe_output_path(chart_path: Path, filename: str) -> Path:
    """Return a direct child output path, rejecting symlink redirection."""
    target = chart_path / filename
    if target.is_symlink():
        raise UpgradeError(f"refusing to write through symlink: {target}")
    try:
        target.parent.resolve(strict=True).relative_to(chart_path.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise UpgradeError(f"output escapes chart directory: {target}") from exc
    return target
