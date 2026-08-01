"""Generic path validation helpers."""

from __future__ import annotations

from pathlib import Path

__all__ = ["ensure_relative", "relative_path"]


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


def relative_path(value: object, *, field: str) -> Path:
    """Require one repository-relative path, judged on how it is spelled.

    Deliberately stricter than `ensure_relative`, which only rejects absolute
    and parent-escaping paths: this also rejects the empty string, backslashes,
    doubled separators and `.` segments, because those are all spellings a
    reader would have to normalize in their head before knowing which file is
    meant. The two rules are not interchangeable.

    Shared by `chart_manager.api.local.v1alpha1`, which applies it to authored
    fields, and by `chart_manager.services.local_resources`, which applies the
    same rule to the layout paths its loader is constructed with. Pure and
    lexical -- it never touches the filesystem, so it says nothing about
    whether the path exists.
    """
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{field} must be a repository-relative path")
    raw = str(value)
    # Check the authored spelling before Path normalizes away "." segments.
    if (
        not raw
        or "\\" in raw
        or raw.startswith("/")
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise ValueError(
            f"{field} must be a repository-relative path without empty, '.' or '..' segments"
        )
    path = Path(raw)
    if path.is_absolute():
        raise ValueError(f"{field} must be a repository-relative path")
    return path
