"""Containment-safe chart path resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from chart_manager.services.upgrader.errors import UpgradeError
from chart_manager.settings import DEFAULT_CHARTS_DIR, RepositoryLayout


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
    charts_dir: Path = DEFAULT_CHARTS_DIR,
) -> tuple[Path, Path, dict[str, Any]]:
    """Resolve and validate one chart without allowing an escape from ``root``."""
    try:
        repo_root = root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise UpgradeError(f"repository root does not exist: {root}") from exc
    layout = RepositoryLayout(root=repo_root, charts_dir=charts_dir)
    raw = chart_path.expanduser()
    if raw.is_absolute():
        candidate = raw
    elif len(raw.parts) == 1:
        candidate = layout.chart_path(raw.name)
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
    yaml = YAML(typ="safe")
    try:
        document = yaml.load(chart_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise UpgradeError(f"invalid Chart.yaml {chart_file}: {exc}") from exc
    if not isinstance(document, dict):
        raise UpgradeError(f"Chart.yaml must contain a mapping: {chart_file}")
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
