"""Which charts a change set publishes."""

from __future__ import annotations

from collections.abc import Iterable

from chart_manager.shared.charts.chart import ChartRepository
from chart_manager.shared.workspace import RepositoryWorkspace


def select(changes: Iterable[str], *, workspace: RepositoryWorkspace) -> tuple[str, ...]:
    """The current charts that own a changed path, sorted.

    Ownership only: publishing does not follow chart-test fanout, `dependentTests` or Helm
    dependents. Paths are relative to the workspace root, not the git top level.
    """
    charts = ChartRepository(workspace.root, charts_dir=workspace.spec.charts_dir)
    current = set(charts.list_names())
    return tuple(
        sorted(
            {
                name
                for path in (raw.strip() for raw in changes)
                if path
                if (name := workspace.chart_name_from_repo_path(path)) is not None
                if name in current
            }
        )
    )
