from __future__ import annotations

from pathlib import Path

from chart_manager.integrations.git import Git
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.charts import ChartRepository
from chart_manager.plumbing.errors import ExternalCommandError


class CiService:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repository = ChartRepository(root)
        self.git = Git(root)
        self.helm = Helm()
        self.kubectl = Kubectl()

    def changed_charts(self, base: str = "origin/main") -> list[str]:
        known = set(self.repository.list_names())
        return [chart for chart in self.git.changed_charts(base) if chart in known]

    def install_source_chart(self, chart_name: str, profile: str, namespace: str) -> None:
        chart = self.repository.get(chart_name)
        profile_spec = chart.spec.profile(profile)
        values = self.repository.value_paths(chart, profile)
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
        # Two-phase install to exercise the upgrade path: deploy the
        # published baseline from OCI, then upgrade to the local source.
        # Both phases use the same values so the baseline release matches
        # what's running in production rather than chart defaults.
        chart = self.repository.get(chart_name)
        profile_spec = chart.spec.profile(profile)
        values = self.repository.value_paths(chart, profile)
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
