"""Safe filesystem paths for rendered validation output."""

from __future__ import annotations

import shutil
from pathlib import Path, PureWindowsPath

from chart_manager.plumbing.errors import SpecError


def validate_path_segment(value: str, *, label: str) -> str:
    """Return ``value`` when it is one safe, non-empty path segment.

    Both POSIX and Windows separators are rejected regardless of the host
    platform. This keeps authored identifiers portable and prevents a value
    from becoming more than one directory component on another platform.
    """
    if not value or not value.strip():
        raise SpecError(f"{label} must be a non-empty path segment")
    if value in {".", ".."}:
        raise SpecError(f"unsafe {label} path segment: {value!r}")
    if any(ord(character) < 32 for character in value):
        raise SpecError(f"unsafe {label} path segment: {value!r}")
    if "/" in value or "\\" in value:
        raise SpecError(f"unsafe {label} path segment: {value!r}")
    windows_path = PureWindowsPath(value)
    if Path(value).is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise SpecError(f"{label} must not be an absolute path: {value!r}")
    return value


def case_output_directory(
    output_root: Path,
    *,
    chart: str,
    environment: str,
) -> Path:
    """Resolve and validate ``output_root/chart/environment``.

    Existing symlinks in either identifier component are rejected, even when
    they happen to resolve within ``output_root``. Following one would make
    cleanup target a directory other than the deterministic case path.
    """
    root = output_root.resolve()
    chart_segment = validate_path_segment(chart, label="chart")
    environment_segment = validate_path_segment(environment, label="environment")
    chart_dir = root / chart_segment
    target = chart_dir / environment_segment

    for component, label in (
        (chart_dir, "chart output directory"),
        (target, "environment output directory"),
    ):
        if component.is_symlink():
            raise SpecError(f"{label} must not be a symlink: {component}")

    resolved = target.resolve()
    if not resolved.is_relative_to(root):
        raise SpecError(
            f"validation output escapes configured root: {resolved} is not under {root}"
        )
    return resolved


def reset_case_output_directory(
    output_root: Path,
    *,
    chart: str,
    environment: str,
) -> Path:
    """Safely clear and recreate one containment-checked case directory."""
    target = case_output_directory(
        output_root,
        chart=chart,
        environment=environment,
    )
    if target.exists():
        if not target.is_dir():
            raise SpecError(f"validation output path is not a directory: {target}")
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=False)
    return target
