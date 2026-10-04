"""Every resource file checked into the repo loads through its production loader."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from chart_manager.commands.validate.schemas.lock import load_schema_lock
from chart_manager.shared.charts.lifecycle import LIFECYCLE_FILENAME, load_chart_lifecycle
from chart_manager.shared.cluster.local_cluster import load_local_cluster
from chart_manager.shared.workspace import load_repository_workspace

from .conftest import REPO_ROOT

_LOADERS: dict[str, Callable[[Path], object]] = {
    LIFECYCLE_FILENAME: load_chart_lifecycle,
    "local-cluster.yaml": load_local_cluster,
    "schemas.lock.yaml": load_schema_lock,
    "workspace.yaml": lambda path: load_repository_workspace(path.parents[1]),
}

RESOURCE_FILES = sorted(
    [
        *REPO_ROOT.glob(f"charts/*/{LIFECYCLE_FILENAME}"),
        *(REPO_ROOT / ".chart-manager" / name for name in _LOADERS if name != LIFECYCLE_FILENAME),
    ]
)


@pytest.mark.parametrize("path", RESOURCE_FILES, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_resource_file_loads(path: Path) -> None:
    _LOADERS[path.name](path)
