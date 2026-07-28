"""Ephemeral cluster chart testing on a kind cluster.

CI-shaped counterpart to ``DevelopmentClusterService``; see the development
cluster package docstring for the full contrast.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind, kind_context
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.clusters import bootstrap as cluster_bootstrap
from chart_manager.services.clusters.bootstrap import (
    CILIUM_BOOTSTRAP_CHART,
    kind_config_path,
)
from chart_manager.services.domain.install_plan import DependencyResolver, InstallPlanEntry
from chart_manager.services.lifecycle.cluster_executor import (
    ClusterActionExecutor,
    HelmTestResult,
)
from chart_manager.services.lifecycle.compiler import LifecycleCompiler
from chart_manager.services.lifecycle.evidence import ClusterIdentity, LocalEvidenceRepository
from chart_manager.services.lifecycle.models import ActionKind
from chart_manager.services.lifecycle.plan_projection import exclude_bootstrap_owned_charts
from chart_manager.services.progress import ProgressCallback, info, step, warn
from chart_manager.settings import DEFAULT_CHARTS_DIR

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
        charts_dir: Path = DEFAULT_CHARTS_DIR,
    ) -> None:
        """Wire integrations; every cluster-facing collaborator is required.

        See ``DevelopmentClusterService.__init__``: defaulting these silently
        discarded the composition root's cluster configuration.
        """
        self.root = root
        self.cluster_tests = ClusterTestCatalog(root, charts_dir=charts_dir)
        self.resolver = DependencyResolver(self.cluster_tests.get)
        # Share the catalog/resolver instances so authored configuration is
        # loaded consistently and tests/alternate surfaces can replace the
        # repository seams once rather than patching two independent graphs.
        self.lifecycle_compiler = LifecycleCompiler(root, charts_dir=charts_dir)
        self.lifecycle_compiler.cluster_tests = self.cluster_tests
        self.lifecycle_compiler.resolver = self.resolver
        self.evidence_repository = LocalEvidenceRepository(
            root / ".chart-manager" / "state"
        )
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

        # Lint needs the legacy per-entry hook, a Cilium target must retain
        # the bootstrap-owned live control-plane install semantics, and
        # dependent-test fanout currently relies on one shared dedupe set
        # across several plans. Keep those branches explicit until their
        # contracts have first-class lifecycle actions.
        use_legacy = (
            options.lint
            or options.chart == CILIUM_BOOTSTRAP_CHART
            or options.include_dependent_tests
        )
        if use_legacy:
            plan = self.resolver.install_plan(options.chart, options.profile)
            self._install_plan(
                plan,
                options,
                installed,
                tested,
                namespaces_created,
                lint=options.lint,
            )
        else:
            self._execute_lifecycle_plan(
                options,
                installed=installed,
                tested=tested,
                namespaces_created=namespaces_created,
            )

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

    def _execute_lifecycle_plan(
        self,
        options: EphemeralTestRequest,
        *,
        installed: set[str],
        tested: list[str],
        namespaces_created: set[str],
    ) -> None:
        """Execute the safe default path through the compiled lifecycle DAG."""

        compiled = self.lifecycle_compiler.compile_cluster_test(
            options.chart,
            options.profile,
            default_namespace=options.namespace,
        )
        plan = exclude_bootstrap_owned_charts(
            compiled,
            {CILIUM_BOOTSTRAP_CHART},
        )
        executor = ClusterActionExecutor(
            helm=_ExecutorHelmAdapter(self.helm),
            kubectl=self.kubectl,
            repository=self.evidence_repository,
        )
        context = getattr(self.kubectl, "context", None) or kind_context(
            options.cluster_name
        )
        result = executor.execute(
            plan,
            fail_fast=True,
            run_id=_new_lifecycle_run_id(),
            cluster=ClusterIdentity(name=options.cluster_name, context=context),
        )
        for diagnostic in result.diagnostics:
            self._progress(
                warn(
                    f"lifecycle evidence write failed for "
                    f"{diagnostic.action_id}: {diagnostic.message}"
                )
            )

        for outcome in result.outcomes:
            if outcome.verdict != "PASS":
                continue
            action = plan.action(outcome.action_id)
            if action.kind is ActionKind.NAMESPACE_ENSURE:
                if action.target.namespace is not None:
                    namespaces_created.add(action.target.namespace)
            elif action.kind is ActionKind.HELM_UPGRADE_INSTALL:
                installed.add(action.target.release or action.target.chart)
            elif action.kind is ActionKind.HELM_TEST:
                tested.append(action.target.chart)

        failure = next(
            (outcome for outcome in result.outcomes if outcome.verdict == "FAIL"),
            None,
        )
        if failure is None:
            return
        failed_action = plan.action(failure.action_id)
        namespace = failed_action.target.namespace
        if namespace is not None:
            diagnostics = self.kubectl.diagnostics(namespace)
            if diagnostics.strip():
                self._progress(info(diagnostics))
        if failed_action.kind is ActionKind.HELM_TEST:
            raise ChartManagerError(
                f"helm test failed for {failed_action.target.chart}: {failure.detail}"
            )
        raise ChartManagerError(
            f"cluster action failed for {failed_action.target.chart} "
            f"({failed_action.kind.value}): {failure.detail}"
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


def _new_lifecycle_run_id() -> str:
    """Mint one evidence-safe identity for an ephemeral cluster-test run."""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"cluster-test-{timestamp}-{uuid4().hex[:12]}"


class _ExecutorHelmAdapter:
    """Narrow the production Helm API to the executor's structural port."""

    def __init__(self, helm: Helm) -> None:
        self.helm = helm

    def dependency_update_if_stale(self, chart_path: Path) -> object:
        return self.helm.dependency_update_if_stale(chart_path)

    def upgrade_install(
        self,
        release: str,
        chart_path: Path,
        *,
        namespace: str,
        values: list[Path] | None,
        timeout: str,
        wait: bool,
    ) -> object:
        return self.helm.upgrade_install(
            release,
            chart_path,
            namespace=namespace,
            values=values,
            timeout=timeout,
            wait=wait,
        )

    def test(
        self,
        release: str,
        *,
        namespace: str,
        timeout: str,
    ) -> HelmTestResult:
        return cast(
            HelmTestResult,
            self.helm.test(release, namespace=namespace, timeout=timeout),
        )
