from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.charts import ChartRepository
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.plumbing.graph import DependencyResolver, PlanEntry
from chart_manager.services import cluster_bootstrap
from chart_manager.services.cluster_bootstrap import (
    CILIUM_BOOTSTRAP_CHART,
    KIND_CONFIG_FILENAME,
)

DEFAULT_CLUSTER_NAME = "chart-manager"
DEFAULT_NAMESPACE = "observability"
DEFAULT_PROFILE = "minimal"


@dataclass(frozen=True)
class SandboxOptions:
    chart: str
    profile: str = DEFAULT_PROFILE
    namespace: str = DEFAULT_NAMESPACE
    cluster_name: str = DEFAULT_CLUSTER_NAME
    ensure_cluster: bool = True
    include_reverse: bool = False
    lint: bool = False


class SandboxService:
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

    def run(self, options: SandboxOptions) -> None:
        if options.ensure_cluster:
            self.console.print(f"[bold]Ensuring sandbox cluster[/bold] {options.cluster_name}")
            kind_config = self.root / KIND_CONFIG_FILENAME
            self.kind.ensure_cluster(
                options.cluster_name,
                config=kind_config if kind_config.exists() else None,
            )
            # ensure_cluster may have started stopped node containers
            # (LabService's `down` path leaves them stopped on disk). On
            # that path the apiserver isn't reachable for several seconds
            # even though docker reports the container up, and the very
            # next thing we do is `helm dependency update` / install,
            # which races. Gate explicitly.
            self.console.print("[bold]Waiting for kube-apiserver[/bold]")
            self.kubectl.wait_apiserver_ready()

        installed: set[str] = set()
        namespaces_created: set[str] = set()

        # Delegate to the shared bootstrap module so `sandbox test` and
        # `sandbox up` exercise the exact same CNI install path. The
        # bootstrap returns the helm status string, or None when the
        # cilium chart is absent. Either non-None value means "ran".
        status = cluster_bootstrap.bootstrap(
            options.cluster_name,
            helm=self.helm,
            kind=self.kind,
            kubectl=self.kubectl,
            repository=self.repository,
            console=self.console,
            lint=options.lint,
        )
        if status is not None:
            installed.add(CILIUM_BOOTSTRAP_CHART)
            namespaces_created.add(cluster_bootstrap.CILIUM_BOOTSTRAP_NAMESPACE)

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
        options: SandboxOptions,
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
                # mtime-gated: a CI runner that's just `helm dependency
                # update`d this chart on the previous step sees the lock
                # is fresh and skips the redundant subprocess. Per-process
                # cache also dedupes across the install + reverse-tests
                # passes when both touch the same chart.
                self.helm.dependency_update_if_stale(chart.path)
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
                        result = self.helm.test(
                            release, namespace=namespace, timeout=profile.timeout
                        )
                        if result.returncode != 0:
                            raise ExternalCommandError(
                                f"helm test exited {result.returncode}\n"
                                f"{result.stderr or result.stdout}"
                            )
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
