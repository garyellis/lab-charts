from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from lab_charts.integrations.helm import Helm
from lab_charts.integrations.kind import Kind
from lab_charts.integrations.kubectl import Kubectl
from lab_charts.plumbing.charts import ChartRepository
from lab_charts.plumbing.errors import LabChartsError
from lab_charts.plumbing.graph import DependencyResolver, PlanEntry


@dataclass(frozen=True)
class KindTestOptions:
    chart: str
    profile: str = "minimal"
    namespace: str = "observability"
    cluster_name: str = "lab-charts"
    ensure_cluster: bool = True
    include_reverse: bool = False
    lint: bool = False


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
            self.kind.ensure_cluster(options.cluster_name)

        installed: set[str] = set()
        namespaces_created: set[str] = set()

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
                try:
                    self.helm.upgrade_install(
                        release,
                        chart.path,
                        namespace=namespace,
                        values=values,
                        timeout=profile.timeout,
                        wait=False,
                    )
                except Exception:
                    diagnostics = self.kubectl.diagnostics(namespace)
                    if diagnostics.strip():
                        self.console.print(diagnostics)
                    raise
                installed.add(release)

            if profile.helm_test:
                self.console.print(f"[bold]Waiting for workloads[/bold] {entry.chart}")
                self.kubectl.wait_workloads_ready(namespace, timeout=profile.timeout)
                self.console.print(f"[bold]Running helm test[/bold] {entry.chart}")
                try:
                    self.helm.test(release, namespace=namespace, timeout=profile.timeout)
                except Exception as exc:
                    diagnostics = self.kubectl.diagnostics(namespace)
                    if diagnostics.strip():
                        self.console.print(diagnostics)
                    raise LabChartsError(f"helm test failed for {entry.chart}: {exc}") from exc
