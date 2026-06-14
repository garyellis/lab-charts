from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from rich.console import Console

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.charts import ChartRepository
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.plumbing.graph import DependencyResolver, PlanEntry

DEFAULT_CLUSTER_NAME = "chart-manager"
DEFAULT_NAMESPACE = "observability"
DEFAULT_PROFILE = "minimal"


@dataclass(frozen=True)
class KindTestOptions:
    chart: str
    profile: str = DEFAULT_PROFILE
    namespace: str = DEFAULT_NAMESPACE
    cluster_name: str = DEFAULT_CLUSTER_NAME
    ensure_cluster: bool = True
    include_reverse: bool = False
    lint: bool = False


# Cilium runs as the kind cluster CNI (with full kube-proxy replacement),
# so it must come up before anything else can become Ready. Bootstrap
# settings here -- not in test-spec.yaml -- because they're a property
# of the kind environment, not of the cilium chart's test contract.
CILIUM_BOOTSTRAP_CHART = "cilium"
CILIUM_BOOTSTRAP_PROFILE = DEFAULT_PROFILE
CILIUM_BOOTSTRAP_NAMESPACE = "kube-system"
CILIUM_BOOTSTRAP_TIMEOUT = "10m"
KIND_CONFIG_FILENAME = "kind-config.yaml"


class KindTestService:
    def __init__(
        self,
        root: Path,
        *,
        helm: Helm | None = None,
        kind: Kind | None = None,
        kubectl: Kubectl | None = None,
        console: Console | None = None,
    ) -> None:
        self.root = root
        self.repository = ChartRepository(root)
        self.resolver = DependencyResolver(self.repository)
        self.helm = helm or Helm()
        self.kind = kind or Kind()
        self.kubectl = kubectl or Kubectl()
        self.console = console or Console()

    def run(self, options: KindTestOptions) -> None:
        if options.ensure_cluster:
            self.console.print(f"[bold]Ensuring kind cluster[/bold] {options.cluster_name}")
            kind_config = self.root / KIND_CONFIG_FILENAME
            self.kind.ensure_cluster(
                options.cluster_name,
                config=kind_config if kind_config.exists() else None,
            )

        installed: set[str] = set()
        namespaces_created: set[str] = set()

        self._bootstrap_cilium(options, installed, namespaces_created, lint=options.lint)

        plan = self.resolver.install_plan(options.chart, options.profile)
        self._install_plan(
            plan, options, installed, namespaces_created, lint=options.lint
        )

        if options.include_reverse:
            for reverse in self.resolver.reverse_tests(options.chart):
                reverse_plan = self.resolver.install_plan(reverse.chart, reverse.profile)
                self._install_plan(
                    reverse_plan, options, installed, namespaces_created, lint=options.lint
                )

    def _bootstrap_cilium(
        self,
        options: KindTestOptions,
        installed: set[str],
        namespaces_created: set[str],
        *,
        lint: bool,
    ) -> None:
        try:
            chart = self.repository.get(CILIUM_BOOTSTRAP_CHART)
        except ChartManagerError:
            self.console.print("[yellow]cilium chart not found; skipping CNI bootstrap[/yellow]")
            return

        api_ip = self.kind.control_plane_ip(options.cluster_name)
        values = self.repository.value_paths(chart, CILIUM_BOOTSTRAP_PROFILE)

        self.console.print(
            f"[bold]Bootstrapping cilium CNI[/bold] "
            f"(k8sServiceHost={api_ip}, namespace={CILIUM_BOOTSTRAP_NAMESPACE})"
        )
        self.helm.dependency_update(chart.path)
        if lint:
            self.helm.lint(chart.path, values)

        namespaces_created.add(CILIUM_BOOTSTRAP_NAMESPACE)
        with self._diagnostics_on_failure(CILIUM_BOOTSTRAP_NAMESPACE):
            self.helm.upgrade_install(
                CILIUM_BOOTSTRAP_CHART,
                chart.path,
                namespace=CILIUM_BOOTSTRAP_NAMESPACE,
                values=values,
                sets={
                    "cilium.k8sServiceHost": api_ip,
                    "cilium.k8sServicePort": "6443",
                },
                timeout=CILIUM_BOOTSTRAP_TIMEOUT,
                wait=False,
            )

        # Block until cilium-agent (daemonset) and coredns (deployment) are
        # rolled out -- coredns can only become Ready once cilium is wiring
        # pod networking, so this is also our "nodes are usable" gate.
        self.console.print("[bold]Waiting for kube-system workloads[/bold] (cilium, coredns)")
        self.kubectl.wait_workloads_ready(
            CILIUM_BOOTSTRAP_NAMESPACE, timeout=CILIUM_BOOTSTRAP_TIMEOUT
        )

        installed.add(CILIUM_BOOTSTRAP_CHART)

    def _install_plan(
        self,
        plan: list[PlanEntry],
        options: KindTestOptions,
        installed: set[str],
        namespaces_created: set[str],
        *,
        lint: bool,
    ) -> None:
        for entry in plan:
            chart = self.repository.get(entry.chart)
            profile = chart.spec.profile(entry.profile)
            values = self.repository.value_paths(chart, entry.profile)
            release = entry.chart
            namespace = profile.namespace or options.namespace

            if namespace not in namespaces_created:
                self.kubectl.create_namespace(namespace)
                namespaces_created.add(namespace)

            if release not in installed:
                self.console.print(f"[bold]Updating dependencies[/bold] {entry.chart}")
                self.helm.dependency_update(chart.path)
                if lint:
                    self.console.print(f"[bold]Linting[/bold] {entry.chart}")
                    self.helm.lint(chart.path, values)
                self.console.print(
                    f"[bold]Installing[/bold] {entry.chart}:{entry.profile} -> {namespace}"
                )
                with self._diagnostics_on_failure(namespace):
                    self.helm.upgrade_install(
                        release,
                        chart.path,
                        namespace=namespace,
                        values=values,
                        timeout=profile.timeout,
                        wait=False,
                    )
                installed.add(release)

            if profile.helm_test:
                self.console.print(f"[bold]Waiting for workloads[/bold] {entry.chart}")
                self.kubectl.wait_workloads_ready(namespace, timeout=profile.timeout)
                self.console.print(f"[bold]Running helm test[/bold] {entry.chart}")
                try:
                    with self._diagnostics_on_failure(namespace):
                        self.helm.test(release, namespace=namespace, timeout=profile.timeout)
                except ExternalCommandError as exc:
                    raise ChartManagerError(f"helm test failed for {entry.chart}: {exc}") from exc

    @contextmanager
    def _diagnostics_on_failure(self, namespace: str) -> Iterator[None]:
        try:
            yield
        except ExternalCommandError:
            diagnostics = self.kubectl.diagnostics(namespace)
            if diagnostics.strip():
                self.console.print(diagnostics)
            raise
