"""Ephemeral cluster chart testing on a kind cluster.

CI-shaped counterpart to ``DevelopmentClusterService``; see the development
cluster package docstring for the full contrast.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.clusters import bootstrap as cluster_bootstrap
from chart_manager.services.clusters.bootstrap import (
    CILIUM_BOOTSTRAP_CHART,
    kind_config_path,
)
from chart_manager.services.domain.install_plan import DependencyResolver, InstallPlanEntry
from chart_manager.services.progress import ProgressCallback, info, step

DEFAULT_CLUSTER_NAME = "chart-manager"
DEFAULT_NAMESPACE = "observability"
DEFAULT_PROFILE = "minimal"


@dataclass(frozen=True)
class EphemeralTestRequest:
    """One ephemeral chart-test invocation."""

    chart: str
    profile: str = DEFAULT_PROFILE
    namespace: str = DEFAULT_NAMESPACE
    cluster_name: str = DEFAULT_CLUSTER_NAME
    ensure_cluster: bool = True
    include_dependent_tests: bool = False
    lint: bool = False


@dataclass(frozen=True)
class EphemeralTestResult:
    """What one `EphemeralTestClusterService.run` actually did.

    `ok` is trivially True: this service is fail-fast, so any chart error
    propagates out of `run` and no result is produced at all. The property
    exists so every surface can branch on the same attribute regardless of
    which service it called.
    """

    chart: str
    profile: str
    cluster_name: str
    installed: tuple[str, ...] = ()
    tested: tuple[str, ...] = ()
    namespaces: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True whenever a result exists -- failures raise instead of returning."""
        return True


class EphemeralTestClusterService:
    """Install a chart's dependency plan and run its helm tests, failing fast."""

    def __init__(
        self,
        root: Path,
        *,
        helm: Helm,
        kind: Kind,
        kubectl: Kubectl,
        progress: ProgressCallback | None = None,
    ) -> None:
        """Wire integrations; every cluster-facing collaborator is required.

        See ``DevelopmentClusterService.__init__``: defaulting these silently
        discarded the composition root's cluster configuration.
        """
        self.root = root
        self.cluster_tests = ClusterTestCatalog(root)
        self.resolver = DependencyResolver(self.cluster_tests.get)
        self.helm = helm
        self.kind = kind
        self.kubectl = kubectl
        # No-op default so the narration call sites don't need a None check.
        self._progress: ProgressCallback = progress or (lambda _event: None)

    def ensure_cluster(self, cluster_name: str = DEFAULT_CLUSTER_NAME) -> str:
        """Create or start the sandbox kind cluster; return its name.

        Owns the kind-config rule (see `cluster_bootstrap.kind_config_path`)
        so callers never have to know where the cluster's config comes from.
        `Kind.ensure_cluster` handles the absent/stopped/running cases.
        """
        self._progress(step("Ensuring sandbox cluster", cluster_name))
        self.kind.ensure_cluster(cluster_name, config=kind_config_path(self.root))
        return cluster_name

    def run(self, options: EphemeralTestRequest) -> EphemeralTestResult:
        """Ensure the cluster, bootstrap CNI, install the plan, run helm tests.

        Fail-fast: the first chart error propagates (contrast
        ``DevelopmentClusterService.up``,
        which continues on error). `include_dependent_tests` re-runs the plans
        of charts declared as dependent-test targets. Returns the
        accounting of what was installed and tested; narration goes to the
        injected progress callback.
        """
        if options.ensure_cluster:
            self.ensure_cluster(options.cluster_name)
            # ensure_cluster may have started stopped node containers
            # (DevelopmentClusterService's `down` path leaves them stopped
            # on disk). On
            # that path the apiserver isn't reachable for several seconds
            # even though docker reports the container up, and the very
            # next thing we do is `helm dependency update` / install,
            # which races. Gate explicitly.
            self._progress(step("Waiting for kube-apiserver"))
            self.kubectl.wait_apiserver_ready()

        installed: set[str] = set()
        tested: list[str] = []
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
            cluster_tests=self.cluster_tests,
            progress=self._progress,
            lint=options.lint,
        )
        if status is not None:
            installed.add(CILIUM_BOOTSTRAP_CHART)
            namespaces_created.add(cluster_bootstrap.CILIUM_BOOTSTRAP_NAMESPACE)

        plan = self.resolver.install_plan(options.chart, options.profile)
        self._install_plan(plan, options, installed, tested, namespaces_created, lint=options.lint)

        if options.include_dependent_tests:
            for dependent in self.resolver.dependent_tests(options.chart):
                dependent_plan = self.resolver.install_plan(
                    dependent.chart,
                    dependent.profile,
                )
                self._install_plan(
                    dependent_plan,
                    options,
                    installed,
                    tested,
                    namespaces_created,
                    lint=options.lint,
                )

        return EphemeralTestResult(
            chart=options.chart,
            profile=options.profile,
            cluster_name=options.cluster_name,
            installed=tuple(sorted(installed)),
            tested=tuple(tested),
            namespaces=tuple(sorted(namespaces_created)),
        )

    def _install_plan(
        self,
        plan: list[InstallPlanEntry],
        options: EphemeralTestRequest,
        installed: set[str],
        tested: list[str],
        namespaces_created: set[str],
        *,
        lint: bool,
    ) -> None:
        """Install each plan entry once, then `helm test` where the profile asks.

        `installed` / `tested` / `namespaces_created` are mutated so work is
        deduped across the main and dependent-test passes; helm tests still
        re-run for already-installed charts (that is the point of dependent
        tests).
        """
        for entry in plan:
            chart = self.cluster_tests.get(entry.chart)
            profile = chart.spec.profile(entry.profile)
            values = self.cluster_tests.value_paths(chart, entry.profile)
            release = entry.chart
            namespace = profile.namespace or options.namespace

            if namespace not in namespaces_created:
                self.kubectl.create_namespace(namespace)
                namespaces_created.add(namespace)

            if release not in installed:
                self._progress(step("Updating dependencies", entry.chart))
                # mtime-gated: a CI runner that's just `helm dependency
                # update`d this chart on the previous step sees the lock
                # is fresh and skips the redundant subprocess. Per-process
                # cache also dedupes across the install + dependent tests
                # passes when both touch the same chart.
                self.helm.dependency_update_if_stale(chart.path)
                if lint:
                    self._progress(step("Linting", entry.chart))
                    self.helm.lint(chart.path, values)
                self._progress(step("Installing", f"{entry.chart}:{entry.profile} -> {namespace}"))
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
                self._progress(step("Waiting for workloads", entry.chart))
                self.kubectl.wait_workloads_ready(namespace, timeout=profile.timeout)
                self._progress(step("Running helm test", entry.chart))
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
                tested.append(entry.chart)

    @contextmanager
    def _diagnostics_on_failure(self, namespace: str) -> Iterator[None]:
        """Emit pod/event diagnostics on ExternalCommandError, then re-raise."""
        try:
            yield
        except ExternalCommandError:
            diagnostics = self.kubectl.diagnostics(namespace)
            if diagnostics.strip():
                self._progress(info(diagnostics))
            raise
