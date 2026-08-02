"""CiService -- CI selection verbs: change detection and cluster-test matrices."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from chart_manager.domain.charts import ChartRepository
from chart_manager.domain.cluster_tests import ClusterTestCatalog
from chart_manager.integrations.git import Git
from chart_manager.plumbing.errors import (
    CapabilityUnavailableError,
    SpecError,
)
from chart_manager.services.lifecycle.impact import (
    ClusterTestImpact,
    LifecycleImpact,
    LifecycleImpactService,
)
from chart_manager.settings import DEFAULT_CHARTS_DIR, DEFAULT_LOCAL_CONFIG


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


class ClusterTestMatrixSource(Protocol):
    """The three selectors `_select_cluster_tests` dispatches between.

    Public even though its only caller is private: it is the contract a
    non-`CiService` matrix source (a stored plan, a webhook payload) would
    have to satisfy, and it is what lets the dispatch be exercised against a
    stand-in without constructing a repo-backed `CiService`. A `Protocol` is
    a type, not a second entry point -- `CiService.matrix` remains the only
    way to *run* the dispatch.
    """

    def cluster_test_matrix(self, base: str) -> tuple[ClusterTestImpact, ...]:
        """Entries selected by diffing against `base`."""
        ...

    def all_cluster_test_matrix(self) -> tuple[ClusterTestImpact, ...]:
        """Every chart with cluster tests enabled."""
        ...

    def explicit_cluster_test_matrix(
        self,
        charts: list[str],
    ) -> tuple[ClusterTestImpact, ...]:
        """Exactly the named charts, rejecting the complete bad set."""
        ...


def _select_cluster_tests(
    source: ClusterTestMatrixSource,
    selection: MatrixSelection,
) -> tuple[ClusterTestImpact, ...]:
    """Resolve a `MatrixSelection` against a matrix source.

    Private: the implementation of `CiService.matrix`, which is the one
    entry point surfaces call. It stays a free function rather than being
    inlined into the method for one reason -- taking `source` as a parameter
    is what lets the precedence rules be tested against a stand-in, and lets
    a CLI test double reuse the real dispatch instead of reimplementing it.
    A double that reimplements the thing under test passes while the real
    code is broken.

    Precedence is `all_charts` > explicit `charts` > diff against `base`.
    Whether `all_charts` and `charts` together is an *error* is a usage
    question that only a surface can classify -- Click exits 2 for a bad flag
    combination, and an HTTP surface would answer 400 -- so the exclusivity
    check stays at the surface and this function documents the fallback
    rather than raising.
    """
    if selection.all_charts:
        return source.all_cluster_test_matrix()
    if selection.charts:
        return source.explicit_cluster_test_matrix(list(selection.charts))
    return source.cluster_test_matrix(selection.base)


class CiService:
    """CI pipeline verbs for a single chart against an already-provisioned cluster."""

    def __init__(
        self,
        root: Path,
        *,
        charts_dir: Path = DEFAULT_CHARTS_DIR,
        local_config: Path = DEFAULT_LOCAL_CONFIG,
    ) -> None:
        """Wire repository/git against `root`.

        `Git` is constructed inline: it is addressed by `root`, which this
        service already owns.
        """
        self.root = root
        self.cluster_tests = ClusterTestCatalog(root, charts_dir=charts_dir)
        self.charts = ChartRepository(root, charts_dir=charts_dir)
        self.impact = LifecycleImpactService(
            root,
            charts_dir=charts_dir,
            local_config=local_config,
        )
        self.git = Git(root, charts_dir=charts_dir)

    def directly_changed_charts(self, changed_files: Path) -> list[str]:
        """Select chart owners from an explicit newline-delimited file list.

        This is deliberately a lexical projection: publishing must not inherit
        lifecycle capability, dependency fanout, Renovate, or Git policy.
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
            if (name := self.charts.layout.chart_name_from_repo_path(raw.strip()))
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

    def matrix(self, selection: MatrixSelection) -> tuple[ClusterTestImpact, ...]:
        """Resolve any `MatrixSelection` -- the one entry point for a matrix.

        Every surface calls this. Previously `cli/main.py` ran the three-way
        precedence itself, which meant a second surface (REST, Slack, a
        workflow step) had to reproduce `all` > `--chart` > diff exactly, and
        getting it subtly wrong would silently test the wrong charts.

        The three selectors below stay public because they are meaningfully
        different questions; this is the door a caller uses when the answer
        depends on flags it was handed rather than on a question it asked.
        """
        return _select_cluster_tests(self, selection)

    def cluster_test_matrix(
        self,
        base: str = "origin/main",
    ) -> tuple[ClusterTestImpact, ...]:
        """Return exact chart/profile matrix entries with selection reasons."""
        return self.lifecycle_impact(base).cluster_tests

    def all_cluster_test_matrix(self) -> tuple[ClusterTestImpact, ...]:
        """Return every enabled chart with its spec-derived default profile."""
        return tuple(
            ClusterTestImpact(
                chart=name,
                profile=self._default_profile(name),
                reasons=(),
            )
            for name in self.cluster_tests.enabled_names()
        )

    def explicit_cluster_test_matrix(
        self,
        charts: list[str] | tuple[str, ...],
    ) -> tuple[ClusterTestImpact, ...]:
        """Resolve explicitly requested charts or reject the complete bad set."""
        requested = sorted(set(charts))
        known = set(self.cluster_tests.repository.list_names())
        unknown = [chart for chart in requested if chart not in known]
        unavailable: list[str] = []
        selected: list[ClusterTestImpact] = []
        for chart in requested:
            if chart in unknown:
                continue
            try:
                profile = self._default_profile(chart)
            except CapabilityUnavailableError:
                unavailable.append(chart)
                continue
            selected.append(
                ClusterTestImpact(
                    chart=chart,
                    profile=profile,
                    reasons=(),
                )
            )
        if unknown or unavailable:
            details = []
            if unknown:
                details.append(f"unknown chart(s): {', '.join(unknown)}")
            if unavailable:
                details.append(
                    "chart(s) without enabled cluster tests: "
                    f"{', '.join(unavailable)}"
                )
            raise SpecError("invalid cluster-test chart request: " + "; ".join(details))
        return tuple(selected)

    def _default_profile(self, chart_name: str) -> str:
        """Delegate shared default selection to lifecycle impact policy."""
        return self.impact.default_cluster_test_profile(chart_name)
