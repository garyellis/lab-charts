"""Generic Helm bootstrap for repository-defined local Kubernetes clusters.

The core deliberately has no knowledge of CNI implementations. A
``LocalCluster`` declares an ordered list of local lifecycle, raw local, or
pinned OCI releases. This executor applies them after the Kind API is ready
and before workload convergence begins.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.api.local.v1alpha1 import (
    BootstrapLifecycleRelease,
    BootstrapLocalChartRelease,
    BootstrapOciChartRelease,
    BootstrapRelease,
    LocalCluster,
)
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.yaml_files import load_yaml_file
from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.clusters.environment import EnvironmentHandle
from chart_manager.services.domain.cluster_test_policy import require_cluster_test_profile
from chart_manager.services.domain.install_plan import DependencyResolver, InstallPlanEntry
from chart_manager.services.lifecycle.plan_projection import ExternallySatisfiedLifecycle
from chart_manager.services.progress import ProgressCallback, emit, step

DEFAULT_NAMESPACE = "default"
DEFAULT_TIMEOUT = "10m"
_RUNTIME_FACTS = {
    "${kind.controlPlanePort}": "6443",
}


@dataclass(frozen=True)
class BootstrapOutcome:
    """One successfully converged bootstrap release."""

    name: str
    profile: str
    namespace: str
    status: str


class LocalBootstrapExecutor:
    """Fail-fast executor for the ordered bootstrap section of LocalCluster."""

    def __init__(
        self,
        root: Path,
        *,
        helm: Helm,
        kind: Kind,
        kubectl: Kubectl,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.root = root.resolve()
        self.helm = helm
        self.kind = kind
        self.kubectl = kubectl
        self.progress = progress

    def execute(
        self,
        cluster: LocalCluster,
        *,
        environment: EnvironmentHandle,
    ) -> tuple[BootstrapOutcome, ...]:
        outcomes: list[BootstrapOutcome] = []
        for release in cluster.spec.bootstrap.releases:
            sets = self._runtime_values(release, environment)
            if isinstance(release, BootstrapLifecycleRelease):
                outcomes.extend(self._install_lifecycle(release, sets=sets))
            elif isinstance(release, BootstrapLocalChartRelease):
                outcomes.append(self._install_local(release, sets=sets))
            elif isinstance(release, BootstrapOciChartRelease):
                outcomes.append(self._install_oci(release, sets=sets))
            else:  # pragma: no cover - the strict discriminated union prevents this
                raise ChartManagerError(f"unsupported bootstrap release: {release!r}")
            self._wait_ready(release)
        return tuple(outcomes)

    def preflight(
        self,
        cluster: LocalCluster,
        *,
        lint: bool = False,
    ) -> frozenset[ExternallySatisfiedLifecycle]:
        """Resolve every lifecycle bootstrap plan before cluster mutation.

        The returned path/chart/profile/namespace identities let workload
        convergence exclude only the exact managed releases already owned by
        bootstrap. All lifecycle releases are resolved before optional Helm
        preparation and linting begins.
        """
        identities: set[ExternallySatisfiedLifecycle] = set()
        lint_targets: list[tuple[Path, list[Path]]] = []
        for release in cluster.spec.bootstrap.releases:
            if not isinstance(release, BootstrapLifecycleRelease):
                continue
            catalog, plan = self._lifecycle_plan(release)
            for entry in plan:
                chart = catalog.get(entry.chart)
                profile = require_cluster_test_profile(chart.spec, entry.profile)
                values = catalog.value_paths(chart, entry.profile)
                namespace = profile.namespace or DEFAULT_NAMESPACE
                identities.add(
                    ExternallySatisfiedLifecycle(
                        chart_path=chart.path.resolve(),
                        chart=entry.chart,
                        profile=entry.profile,
                        namespace=namespace,
                    )
                )
                lint_targets.append((chart.path, values))
        if lint:
            for chart_path, values in lint_targets:
                self.helm.dependency_update_if_stale(chart_path)
                self.helm.lint(chart_path, values)
        return frozenset(identities)

    def _install_lifecycle(
        self,
        release: BootstrapLifecycleRelease,
        *,
        sets: dict[str, str],
    ) -> list[BootstrapOutcome]:
        catalog, plan = self._lifecycle_plan(release)
        chart_name = self._chart_name(release.chart)
        outcomes: list[BootstrapOutcome] = []
        for entry in plan:
            entry_chart = catalog.get(entry.chart)
            profile = require_cluster_test_profile(entry_chart.spec, entry.profile)
            namespace = profile.namespace or DEFAULT_NAMESPACE
            is_bootstrap_root = (
                entry.chart == chart_name and entry.profile == release.profile
            )
            runtime_sets = sets if is_bootstrap_root else {}
            emit(
                self.progress,
                step("Bootstrapping", f"{entry.chart}:{entry.profile} -> {namespace}"),
            )
            self.helm.dependency_update_if_stale(entry_chart.path)
            result = self.helm.upgrade_install(
                entry.chart,
                entry_chart.path,
                namespace=namespace,
                values=catalog.value_paths(entry_chart, entry.profile),
                sets=runtime_sets,
                timeout=profile.timeout,
                wait=False,
            )
            self.kubectl.wait_workloads_ready(namespace, timeout=profile.timeout)
            outcomes.append(
                BootstrapOutcome(entry.chart, entry.profile, namespace, result.status)
            )
        return outcomes

    def _lifecycle_plan(
        self,
        release: BootstrapLifecycleRelease,
    ) -> tuple[ClusterTestCatalog, list[InstallPlanEntry]]:
        chart_name = self._chart_name(release.chart)
        catalog = ClusterTestCatalog(self.root, charts_dir=release.chart.parent)
        chart = catalog.get(chart_name)
        expected = (self.root / release.chart).resolve()
        if chart.path.resolve() != expected:
            raise ChartManagerError(
                f"bootstrap chart {release.chart} resolved to unexpected path {chart.path}"
            )
        plan = DependencyResolver(catalog.get).install_plan(chart_name, release.profile)
        return catalog, plan

    def _install_local(
        self,
        release: BootstrapLocalChartRelease,
        *,
        sets: dict[str, str],
    ) -> BootstrapOutcome:
        chart = (self.root / release.chart).resolve()
        emit(self.progress, step("Bootstrapping", f"{release.name} -> {release.namespace}"))
        self.helm.dependency_update_if_stale(chart)
        result = self.helm.upgrade_install(
            release.name,
            chart,
            namespace=release.namespace,
            values=[self.root / path for path in release.values],
            sets=sets,
            timeout=release.timeout,
            wait=False,
        )
        return BootstrapOutcome(
            release.name,
            "local",
            release.namespace,
            result.status,
        )

    def _install_oci(
        self,
        release: BootstrapOciChartRelease,
        *,
        sets: dict[str, str],
    ) -> BootstrapOutcome:
        identity = release.version or release.digest or "pinned"
        chart_ref = (
            f"{release.chart}@{release.digest}"
            if release.digest is not None
            else release.chart
        )
        emit(self.progress, step("Bootstrapping", f"{release.name}@{identity}"))
        result = self.helm.upgrade_install(
            release.name,
            chart_ref,
            namespace=release.namespace,
            values=[self.root / path for path in release.values],
            sets=sets,
            timeout=release.timeout,
            wait=False,
            **({"version": release.version} if release.version is not None else {}),
        )
        return BootstrapOutcome(
            release.name,
            identity,
            release.namespace,
            result.status,
        )

    def _runtime_values(
        self,
        release: BootstrapRelease,
        environment: EnvironmentHandle,
    ) -> dict[str, str]:
        if not release.runtime_values:
            return {}
        facts = {
            **_RUNTIME_FACTS,
            "${kind.clusterName}": environment.identity,
            "${kind.context}": environment.context,
        }
        if "${kind.controlPlaneHost}" in release.runtime_values.values():
            facts["${kind.controlPlaneHost}"] = self.kind.control_plane_ip(
                environment.identity
            )
        return {key: facts[value] for key, value in release.runtime_values.items()}

    def _wait_ready(self, release: BootstrapRelease) -> None:
        readiness = release.readiness
        if readiness is None:
            return
        timeout = (
            readiness.workloads_ready.timeout
            if readiness.workloads_ready is not None
            else getattr(release, "timeout", DEFAULT_TIMEOUT)
        )
        if readiness.nodes_ready:
            emit(self.progress, step("Waiting for local cluster nodes"))
            self.kubectl.wait_nodes_ready(timeout=timeout)
        if readiness.workloads_ready is not None:
            gate = readiness.workloads_ready
            emit(self.progress, step("Waiting for bootstrap workloads", gate.namespace))
            self.kubectl.wait_workloads_ready(gate.namespace, timeout=gate.timeout)

    def _chart_name(self, relative: Path) -> str:
        document = load_yaml_file(self.root / relative / "Chart.yaml")
        name = document.get("name")
        if not isinstance(name, str):
            raise ChartManagerError(f"{relative}/Chart.yaml must define a string name")
        return name


__all__ = [
    "DEFAULT_NAMESPACE",
    "DEFAULT_TIMEOUT",
    "BootstrapOutcome",
    "LocalBootstrapExecutor",
]
