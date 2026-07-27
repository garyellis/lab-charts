"""Generic path validation helpers."""

from pathlib import Path


def ensure_relative(
    values: list[str],
    *,
    label: str = "path",
    relation: str = "relative",
) -> list[str]:
    """Reject absolute paths and paths that escape through a parent."""
    for value in values:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"{label} must be {relation}: {value}")
    return values
