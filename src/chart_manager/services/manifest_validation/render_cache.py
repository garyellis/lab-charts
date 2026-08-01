"""The on-disk render cache: where `validate` writes, what `cache clean` removes.

`chart cache clean` used to be a `shutil.rmtree` in the CLI against a path
the CLI spelled out itself, which left two problems. The directory layout
was written twice -- here and in `ManifestValidationService._resolve_out_dir`
-- so the cleaner and the writer could drift; and `--dry-run` had nothing to
print, because "what would be removed" is a derived answer and the surface
is not allowed to derive it.

Both are the same fix: one object that knows the location, can describe what
is there, and can remove it. `state()` is the dry-run answer and `clean()`
is the mutation, and they read the tree the same way, so the plan a caller
is shown cannot disagree with what the removal then does.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Repo-relative location of the render cache. The single source of truth:
#: `ManifestValidationService._resolve_out_dir` composes run directories
#: under it, and `chart cache clean` removes the whole tree.
RENDER_CACHE_DIR = Path(".chart-manager") / "rendered"

__all__ = ["RENDER_CACHE_DIR", "RenderCacheService", "RenderCacheState"]


@dataclass(frozen=True)
class RenderCacheState:
    """What the render cache holds right now."""

    path: Path
    exists: bool
    runs: int

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-safe projection `cache clean --dry-run` emits."""
        return {"path": str(self.path), "exists": self.exists, "runs": self.runs}


class RenderCacheService:
    """Describe and remove the render cache under one repository root."""

    def __init__(self, root: Path) -> None:
        """Resolve the cache location for `root`; touches no disk."""
        self.path = (root / RENDER_CACHE_DIR).resolve()

    def state(self) -> RenderCacheState:
        """Report the cache without changing it.

        `runs` counts immediate children -- one per validate run -- rather
        than every file underneath. It is the unit a reader thinks in, and
        it costs one `iterdir` on a tree that can hold thousands of rendered
        manifests.
        """
        if not self.path.is_dir():
            return RenderCacheState(path=self.path, exists=False, runs=0)
        return RenderCacheState(
            path=self.path,
            exists=True,
            runs=sum(1 for _ in self.path.iterdir()),
        )

    def clean(self) -> RenderCacheState:
        """Remove the cache tree and return the state that was removed.

        Returns the *pre-removal* state so a caller can report what went
        away. `OSError` propagates: a cache that could not be removed is a
        failure the caller has to report, not a state to describe.
        """
        state = self.state()
        if state.exists:
            shutil.rmtree(self.path)
        return state
