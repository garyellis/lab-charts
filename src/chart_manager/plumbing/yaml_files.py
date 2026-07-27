"""Generic YAML file loading helpers."""

from pathlib import Path
from typing import Any

import yaml

from chart_manager.plumbing.errors import SpecError


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Read a YAML file that must contain a mapping; empty file becomes an empty mapping."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except OSError as exc:
        raise SpecError(f"failed to read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"{path} must contain a YAML mapping")
    return data
