"""CiService -- CI selection verbs: change detection and cluster-test matrices."""
from __future__ import annotations

from pathlib import Path

from chart_manager.integrations.git import Git
from chart_manager.plumbing.errors import (
    CapabilityUnavailableError,
    SpecError,
)
from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.domain.charts import ChartRepository
from chart_manager.services.lifecycle.impact import (
    ClusterTestImpact,
    LifecycleImpact,
    LifecycleImpactService,
)
from chart_manager.settings import DEFAULT_CHARTS_DIR, DEFAULT_LOCAL_CONFIG


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
