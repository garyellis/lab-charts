from __future__ import annotations

from pathlib import Path

from lab_charts.integrations.git import Git
from lab_charts.integrations.helm import Helm
from lab_charts.integrations.kubectl import Kubectl
from lab_charts.plumbing.charts import ChartRepository


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
            self.helm.test(chart.name, namespace=namespace, timeout=profile_spec.timeout)

    def upgrade_from_oci(
        self,
        chart_name: str,
        profile: str,
        namespace: str,
        oci_ref: str,
    ) -> None:
        chart = self.repository.get(chart_name)
        profile_spec = chart.spec.profile(profile)
        values = self.repository.value_paths(chart, profile)
        self.kubectl.create_namespace(namespace)
        self.helm.upgrade_install(
            chart.name,
            oci_ref,
            namespace=namespace,
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
            self.helm.test(chart.name, namespace=namespace, timeout=profile_spec.timeout)
