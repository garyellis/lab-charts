"""Generic path validation helpers."""

from __future__ import annotations

import shutil
from pathlib import Path

from chart_manager.plumbing.errors import SpecError

__all__ = ["ensure_relative", "relative_path", "validate_hook_executable"]


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
    fields, and by `chart_manager.domain.local_resources`, which applies the
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


def validate_hook_executable(
    root: Path,
    executable: str,
    *,
    field: str,
    require_on_path: bool = False,
) -> Path | None:
    """Validate a hook's argv[0]; return the repository file it names, if any.

    An executable spelled with a separator is a repository-relative path: it
    must pass `relative_path`, stay inside `root` once symlinks resolve, and
    name an existing file. A bare name is a PATH command and returns None;
    it is looked up only when `require_on_path` is set, because the local
    cluster's provisioning hooks have always left that to run time.
    """
    if "/" not in executable and "\\" not in executable:
        if require_on_path and shutil.which(executable) is None:
            raise SpecError(f"{field} command not found on PATH: {executable}")
        return None
    try:
        authored_path = executable.replace("\\", "/")
        # Hook examples conventionally use `./script`; accept that explicit
        # execution spelling while applying the repository path validator to
        # the path it identifies.
        if authored_path.startswith("./"):
            authored_path = authored_path[2:]
        relative = relative_path(authored_path, field=field)
    except ValueError as exc:
        raise SpecError(str(exc)) from exc
    root = root.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise SpecError(f"path escapes repository root {root}: {relative}")
    if not resolved.is_file():
        raise SpecError(f"{field} file does not exist: {relative}")
    return resolved
