"""Filesystem locations for rendered manifest-validation output.

Two things that name the same tree, so they live together:

  * the per-row case directory a render writes into -- `case_output_directory`
    and `reset_case_output_directory`, plus the segment/containment checks
    (`validate_path_segment`, `require_within`) every authored identifier and
    authored path has to survive before it becomes a directory;
  * the repository-level root those run directories accumulate under --
    `RENDER_OUTPUT_DIR` and `RenderOutputService`, which is what
    `chart cache clean` describes and removes.

They used to be two modules, which let the layout be written twice: the
writer composed it in `ManifestValidationService._resolve_out_dir` and the
cleaner spelled it out again, so the two could drift.

**Nothing here caches.** `reset_case_output_directory` rmtrees and recreates
the case directory for every row -- no key, no hit path, no reuse. The
predecessor module was named `render_cache`, and the name is gone so nobody
builds `--no-cache` on the strength of it. `chart cache clean` keeps its
user-facing spelling; renaming a shipped command is a separate decision.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from chart_manager.plumbing.errors import SpecError

#: Repo-relative root the per-run render directories accumulate under. The
#: single source of truth: `ManifestValidationService._resolve_out_dir`
#: composes run directories below it, and `chart cache clean` removes the
#: whole tree.
RENDER_OUTPUT_DIR = Path(".chart-manager") / "rendered"


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


def require_within(path: Path, base: Path, *, label: str) -> None:
    """Reject resolved local inputs that escape their documented base."""
    if not path.is_relative_to(base):
        raise SpecError(f"{label} escapes its base directory: {path}")


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


def has_manifests(path: Path) -> bool:
    """True if `path` contains at least one real (non-symlink) .yaml/.yml file."""
    # os.walk with followlinks=False avoids infinite recursion on cyclic
    # symlinks. Path.rglob follows symlinked directories by default, which
    # is unsafe against a rendered tree that could contain user-controlled
    # symlinks (helm doesn't emit them today, but the guarantee is cheap).
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            if name.endswith((".yaml", ".yml")):
                # Confirm it's a regular file (not a symlink to one) so we
                # don't count dangling/looping symlink targets.
                full = Path(dirpath) / name
                if full.is_file() and not full.is_symlink():
                    return True
    return False


@dataclass(frozen=True)
class RenderOutputState:
    """What the render output tree holds right now."""

    path: Path
    exists: bool
    runs: int

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-safe projection `cache clean --dry-run` emits."""
        return {"path": str(self.path), "exists": self.exists, "runs": self.runs}


class RenderOutputService:
    """Describe and remove the render output tree under one repository root.

    `chart cache clean` used to be a `shutil.rmtree` in the CLI against a path
    the CLI spelled out itself, which left `--dry-run` with nothing to print:
    "what would be removed" is a derived answer and the surface is not allowed
    to derive it. `state()` is the dry-run answer and `clean()` is the
    mutation, and they read the tree the same way, so the plan a caller is
    shown cannot disagree with what the removal then does.
    """

    def __init__(self, root: Path) -> None:
        """Resolve the output location for `root`; touches no disk."""
        self.path = (root / RENDER_OUTPUT_DIR).resolve()

    def state(self) -> RenderOutputState:
        """Report the tree without changing it.

        `runs` counts immediate children -- one per validate run -- rather
        than every file underneath. It is the unit a reader thinks in, and
        it costs one `iterdir` on a tree that can hold thousands of rendered
        manifests.
        """
        if not self.path.is_dir():
            return RenderOutputState(path=self.path, exists=False, runs=0)
        return RenderOutputState(
            path=self.path,
            exists=True,
            runs=sum(1 for _ in self.path.iterdir()),
        )

    def clean(self) -> RenderOutputState:
        """Remove the tree and return the state that was removed.

        Returns the *pre-removal* state so a caller can report what went
        away. `OSError` propagates: a tree that could not be removed is a
        failure the caller has to report, not a state to describe.
        """
        state = self.state()
        if state.exists:
            shutil.rmtree(self.path)
        return state
