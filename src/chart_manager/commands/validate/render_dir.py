"""The workspace's render directory, where each validate run keeps its rendered manifests."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.errors import SpecError
from chart_manager.shared.workspace import RepositoryWorkspace


@dataclass(frozen=True)
class RenderDirState:
    """Where the render directory is, whether it exists, and how many runs it holds."""

    path: Path
    exists: bool
    runs: int

    def to_dict(self) -> dict[str, object]:
        return {"path": str(self.path), "exists": self.exists, "runs": self.runs}


def render_dir_state(workspace: RepositoryWorkspace) -> RenderDirState:
    """Report the render directory without changing it."""
    path = _render_dir(workspace)
    if not path.is_dir():
        return RenderDirState(path=path, exists=False, runs=0)
    return RenderDirState(path=path, exists=True, runs=sum(1 for _ in path.iterdir()))


def clean_render_dir(workspace: RepositoryWorkspace) -> RenderDirState:
    """Remove the render directory; return what it held. `OSError` propagates."""
    state = render_dir_state(workspace)
    if state.exists:
        shutil.rmtree(_render_dir(workspace))
    return state


def _render_dir(workspace: RepositoryWorkspace) -> Path:
    """The render directory, refusing a symlink on the way or a path outside the repository."""
    root = workspace.root.resolve()
    candidate = root
    for part in workspace.spec.render_dir.parts:
        candidate /= part
        if candidate.is_symlink():
            raise SpecError(f"render output directory must not contain symlinks: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise SpecError(f"render output directory escapes repository root: {candidate}")
    return resolved
