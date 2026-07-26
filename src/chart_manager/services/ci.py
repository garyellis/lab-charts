"""CiService -- per-chart CI verbs: change detection, source install, OCI upgrade path."""
from __future__ import annotations

from pathlib import Path

from chart_manager.integrations.git import Git
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.services.domain.charts import ChartRepository


class CiService:
    """CI pipeline verbs for a single chart against an already-provisioned cluster."""

    def __init__(self, root: Path, *, helm: Helm, kubectl: Kubectl) -> None:
        """Wire repository/git against `root`; take cluster adapters injected.

        `helm`/`kubectl` were constructed inline here, which meant CI ran
        against the ambient kubeconfig no matter what the composition root
        was configured with. `Git` stays inline: it is addressed by `root`,
        which this service already owns.
        """
        self.root = root
        self.repository = ChartRepository(root)
        self.git = Git(root)
        self.helm = helm
        self.kubectl = kubectl

    def changed_charts(self, base: str = "origin/main") -> list[str]:
        """Chart names changed vs `base`, filtered to charts the repository knows."""
        known = set(self.repository.list_names())
        return [chart for chart in self.git.changed_charts(base) if chart in known]

    def install_source_chart(self, chart_name: str, profile: str, namespace: str) -> None:
        """Install the chart from local source, then run `helm test` if the profile enables it.

        Raises ExternalCommandError on a nonzero `helm test`.
        """
        chart = self.repository.get_managed(chart_name)
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
        """Exercise the upgrade path: published OCI baseline, then local source.

        Both phases use the same values so the baseline release matches
        what's running in production rather than chart defaults. Runs
        `helm test` after the upgrade if the profile enables it.
        """
        chart = self.repository.get_managed(chart_name)
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
