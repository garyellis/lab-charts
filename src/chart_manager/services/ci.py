"""CiService -- per-chart CI verbs: change detection, source install, OCI upgrade path."""
from __future__ import annotations

from pathlib import Path

from chart_manager.integrations.git import Git
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import (
    CapabilityUnavailableError,
    ExternalCommandError,
    SpecError,
)
from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.domain.charts import ChartRepository
from chart_manager.services.lifecycle.impact import (
    ClusterTestImpact,
    LifecycleImpact,
    LifecycleImpactService,
)
from chart_manager.settings import DEFAULT_CHARTS_DIR


class CiService:
    """CI pipeline verbs for a single chart against an already-provisioned cluster."""

    def __init__(
        self,
        root: Path,
        *,
        helm: Helm,
        kubectl: Kubectl,
        charts_dir: Path = DEFAULT_CHARTS_DIR,
    ) -> None:
        """Wire repository/git against `root`; take cluster adapters injected.

        `helm`/`kubectl` were constructed inline here, which meant CI ran
        against the ambient kubeconfig no matter what the composition root
        was configured with. `Git` stays inline: it is addressed by `root`,
        which this service already owns.
        """
        self.root = root
        self.cluster_tests = ClusterTestCatalog(root, charts_dir=charts_dir)
        self.charts = ChartRepository(root, charts_dir=charts_dir)
        self.impact = LifecycleImpactService(root, charts_dir=charts_dir)
        self.git = Git(root, charts_dir=charts_dir)
        self.helm = helm
        self.kubectl = kubectl

    def changed_charts(self, base: str = "origin/main") -> list[str]:
        """Compatibility projection of the typed cluster-test matrix.

        This intentionally returns chart names because the existing CI command
        consumes one name per line.  New consumers should use
        :meth:`cluster_test_matrix` so a declared non-minimal dependent profile
        is not silently discarded.
        """
        return sorted({entry.chart for entry in self.cluster_test_matrix(base)})

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

    def cluster_test_charts(self) -> list[str]:
        """Return every chart with enabled live-cluster tests."""
        return self.cluster_tests.enabled_names()

    def install_source_chart(self, chart_name: str, profile: str, namespace: str) -> None:
        """Install the chart from local source, then run `helm test` if the profile enables it.

        Raises ExternalCommandError on a nonzero `helm test`.
        """
        chart = self.cluster_tests.get(chart_name)
        profile_spec = chart.spec.profile(profile)
        values = self.cluster_tests.value_paths(chart, profile)
        self.kubectl.create_namespace(namespace)
        self.helm.dependency_update(chart.path)
        self.helm.upgrade_install(
            chart.name,
            chart.path,
            namespace=namespace,
            values=values,
            timeout=profile_spec.timeout,
        )
        if profile_spec.helm_test:
            result = self.helm.test(chart.name, namespace=namespace, timeout=profile_spec.timeout)
            if result.returncode != 0:
                raise ExternalCommandError(
                    f"helm test failed for {chart.name} "
                    f"({result.returncode}):\n{result.stderr or result.stdout}"
                )

    def upgrade_from_oci(
        self,
        chart_name: str,
        profile: str,
        namespace: str,
        oci_ref: str,
    ) -> None:
        """Exercise the upgrade path: published OCI baseline, then local source.

        Both phases use the same values so the baseline release matches
        what's running in production rather than chart defaults. Runs
        `helm test` after the upgrade if the profile enables it.
        """
        chart = self.cluster_tests.get(chart_name)
        profile_spec = chart.spec.profile(profile)
        values = self.cluster_tests.value_paths(chart, profile)
        self.kubectl.create_namespace(namespace)
        self.helm.upgrade_install(
            chart.name,
            oci_ref,
            namespace=namespace,
            values=values,
            timeout=profile_spec.timeout,
        )
        self.helm.dependency_update(chart.path)
        self.helm.upgrade(
            chart.name,
            chart.path,
            namespace=namespace,
            values=values,
            timeout=profile_spec.timeout,
        )
        if profile_spec.helm_test:
            result = self.helm.test(chart.name, namespace=namespace, timeout=profile_spec.timeout)
            if result.returncode != 0:
                raise ExternalCommandError(
                    f"helm test failed for {chart.name} "
                    f"({result.returncode}):\n{result.stderr or result.stdout}"
                )
