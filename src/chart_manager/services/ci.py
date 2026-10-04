"""CiService -- CI selection verbs: change detection and cluster-test matrices."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.commands import test
from chart_manager.integrations.git import Git
from chart_manager.plumbing.errors import SpecError
from chart_manager.services.lifecycle.impact import LifecycleImpact, LifecycleImpactService
from chart_manager.shared.charts.chart import ChartRepository
from chart_manager.shared.workspace import RepositoryWorkspace


@dataclass(frozen=True)
class MatrixSelection:
    """How a caller wants the cluster-test matrix chosen.

    One value object instead of three positional arguments, so a surface
    states its *intent* and the dispatch below decides which selector runs.
    Previously the surface made that decision with an if/elif chain, which
    meant a second surface had to reproduce the precedence exactly.
    """

    base: str = "origin/main"
    all_charts: bool = False
    charts: tuple[str, ...] = ()


class CiService:
    """CI pipeline verbs for a single chart against an already-provisioned cluster."""

    def __init__(self, *, workspace: RepositoryWorkspace) -> None:
        """Wire repository/git against the workspace root."""
        self.workspace = workspace
        self.root = workspace.root
        self.charts = ChartRepository(self.root, charts_dir=workspace.spec.charts_dir)
        self.impact = LifecycleImpactService(workspace=workspace)
        self.git = Git(self.root)

    def directly_changed_charts(self, changed_files: Path) -> list[str]:
        """Select chart owners from an explicit newline-delimited file list.

        This is deliberately a lexical projection: publishing must not inherit
        lifecycle capability, dependency fanout, Renovate, or Git policy.
        Paths must be relative to the workspace root, not the git top level.
        """
        try:
            paths = changed_files.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SpecError(f"cannot read changed-files input {changed_files}: {exc}") from exc
        current_charts = set(self.charts.list_names())
        selected = {
            name
            for raw in paths
            if raw.strip()
            if (name := self.workspace.chart_name_from_repo_path(raw.strip()))
            is not None
            if name in current_charts
        }
        return sorted(selected)

    def lifecycle_impact(self, base: str = "origin/main") -> LifecycleImpact:
        """Analyze the explicit Git diff and fail loudly on invalid intent."""
        changed_files = self.git.changed_files(base)
        impact = self.impact.analyze(changed_files)
        if impact.spec_errors:
            detail = "\n".join(f"- {error}" for error in impact.spec_errors)
            raise SpecError(f"lifecycle impact analysis found spec errors:\n{detail}")
        return impact

    def matrix(self, selection: MatrixSelection) -> tuple[test.SelectedTest, ...]:
        """The chart tests `selection` asks for: `all` > `--chart` > the diff against `base`.

        `test.select()` holds the selection rules; this only reads the diff from Git.
        """
        if selection.all_charts:
            return test.select(None, workspace=self.workspace).tests
        if selection.charts:
            return test.select(None, workspace=self.workspace, charts=selection.charts).tests
        return self.lifecycle_impact(selection.base).cluster_tests
