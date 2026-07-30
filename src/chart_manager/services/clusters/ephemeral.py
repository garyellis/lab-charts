"""Ephemeral chart testing on a locally configured Kubernetes cluster.

CI-shaped counterpart to ``DevelopmentClusterService``; see the development
cluster package docstring for the full contrast.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind, kind_context
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.clusters.bootstrap import LocalBootstrapExecutor
from chart_manager.services.clusters.environment import (
    EnvironmentHandle,
    EnvironmentSpec,
    KindEnvironmentProvider,
    KubernetesEnvironmentProvider,
)
from chart_manager.services.domain.install_plan import DependencyResolver
from chart_manager.services.lifecycle.cluster_executor import (
    ClusterActionExecutor,
    HelmTestResult,
)
from chart_manager.services.lifecycle.compiler import LifecycleCompiler
from chart_manager.services.lifecycle.evidence import ClusterIdentity, LocalEvidenceRepository
from chart_manager.services.lifecycle.models import (
    ActionKind,
    LifecycleAction,
    LifecyclePlan,
)
from chart_manager.services.lifecycle.plan_projection import (
    ExternallySatisfiedLifecycle,
    exclude_bootstrap_owned_charts,
)
from chart_manager.services.local_resources import LocalCluster, LocalResourceLoader
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
    namespace: str | None = None
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
        local_config: Path = Path(".chart-manager/local-cluster.yaml"),
        environment_provider: KubernetesEnvironmentProvider | None = None,
        client_factory: Callable[[EnvironmentHandle], tuple[Helm, Kubectl]] | None = None,
    ) -> None:
        """Wire integrations; every cluster-facing collaborator is required.

        See ``DevelopmentClusterService.__init__``: defaulting these silently
        discarded the composition root's cluster configuration.
        """
        self.root = root.resolve()
        self.cluster_tests = ClusterTestCatalog(self.root, charts_dir=charts_dir)
        self.resolver = DependencyResolver(self.cluster_tests.get)
        # Share the catalog/resolver instances so authored configuration is
        # loaded consistently and tests/alternate surfaces can replace the
        # repository seams once rather than patching two independent graphs.
        self.lifecycle_compiler = LifecycleCompiler(self.root, charts_dir=charts_dir)
        self.lifecycle_compiler.cluster_tests = self.cluster_tests
        self.lifecycle_compiler.resolver = self.resolver
        self.evidence_repository = LocalEvidenceRepository(
            self.root / ".chart-manager" / "state"
        )
        self.helm = helm
        self.kind = kind
        self.kubectl = kubectl
        self.environment_provider = environment_provider or KindEnvironmentProvider(kind)
        self.local_resources = LocalResourceLoader(self.root, local_config=local_config)
        self._local_cluster: LocalCluster | None = None
        self._client_factory = client_factory
        self._environment_handle: EnvironmentHandle | None = None
        # No-op default so the narration call sites don't need a None check.
        self._progress: ProgressCallback = progress or (lambda _event: None)

    def ensure_cluster(self, cluster_name: str = DEFAULT_CLUSTER_NAME) -> str:
        """Create or start the configured local cluster; return its name.

        ``LocalCluster`` owns the Kind config path and bootstrap sequence;
        callers only select the cluster identity.
        """
        local_cluster = self.local_resources.load_cluster()
        self._local_cluster = local_cluster
        self._progress(step("Ensuring local cluster", cluster_name))
        spec = EnvironmentSpec(
            name=cluster_name,
            cluster_name=cluster_name,
            config=(self.root / local_cluster.spec.cluster.config).resolve(),
        )
        handle = self.environment_provider.ensure(spec)
        self._environment_handle = handle
        if self._client_factory is not None:
            self.helm, self.kubectl = self._client_factory(handle)
        return cluster_name

    def run(self, options: EphemeralTestRequest) -> EphemeralTestResult:
        """Ensure the cluster, run configured bootstrap, install, and test.

        Fail-fast: the first chart error propagates (contrast
        ``DevelopmentClusterService.up``,
        which continues on error). `include_dependent_tests` re-runs the plans
        of charts declared as dependent-test targets. Returns the
        accounting of what was installed and tested; narration goes to the
        injected progress callback.
        """
        # Bootstrap ownership is authored configuration, not process state.
        # Reload it for every run so a long-lived service cannot carry an
        # earlier run's externally-satisfied identities forward.
        local_cluster = self.local_resources.load_cluster()
        self._local_cluster = local_cluster
        bootstrap_preflight = LocalBootstrapExecutor(
            self.root,
            helm=self.helm,
            kind=self.kind,
            kubectl=self.kubectl,
            progress=self._progress,
        )
        bootstrap_lifecycles = bootstrap_preflight.preflight(
            local_cluster,
            lint=options.lint,
        )
        plan = self._compile_lifecycle_plan(
            options,
            bootstrap_lifecycles=bootstrap_lifecycles,
        )
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
        else:
            # The caller owns environment existence, but chart-manager still
            # owns addressing. Never fall back to ambient kubeconfig merely
            # because creation was skipped.
            handle = self.environment_provider.handle(
                EnvironmentSpec(
                    name=options.cluster_name,
                    cluster_name=options.cluster_name,
                    config=(self.root / local_cluster.spec.cluster.config).resolve(),
                )
            )
            self._environment_handle = handle
            if self._client_factory is not None:
                self.helm, self.kubectl = self._client_factory(handle)

        installed: set[str] = set()
        tested: list[str] = []
        namespaces_created: set[str] = set()

        handle = self._environment_handle or self.environment_provider.handle(
            EnvironmentSpec(name=options.cluster_name, cluster_name=options.cluster_name)
        )
        bootstrap = LocalBootstrapExecutor(
            self.root,
            helm=self.helm,
            kind=self.kind,
            kubectl=self.kubectl,
            progress=self._progress,
        )
        for outcome in bootstrap.execute(local_cluster, environment=handle):
            installed.add(outcome.name)
            namespaces_created.add(outcome.namespace)

        self._execute_lifecycle_plan(
            options,
            plan=plan,
            installed=installed,
            tested=tested,
            namespaces_created=namespaces_created,
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
        plan: LifecyclePlan,
        installed: set[str],
        tested: list[str],
        namespaces_created: set[str],
    ) -> None:
        """Execute the safe default path through the compiled lifecycle plan."""

        executor = ClusterActionExecutor(
            helm=_ExecutorHelmAdapter(self.helm),
            kubectl=self.kubectl,
            repository=self.evidence_repository,
            progress=self._progress,
        )
        handle = self._environment_handle
        bound_context = getattr(self.kubectl, "context", None)
        context = (
            handle.context
            if handle is not None
            else bound_context or kind_context(options.cluster_name)
        )
        result = executor.execute(
            plan,
            run_id=_new_lifecycle_run_id(),
            cluster=ClusterIdentity(
                name=handle.identity if handle is not None else options.cluster_name,
                context=context,
            ),
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
            try:
                diagnostics = self.kubectl.diagnostics(namespace)
            except Exception as exc:
                self._progress(
                    warn(
                        f"failed to collect diagnostics for namespace "
                        f"{namespace}: {exc}"
                    )
                )
            else:
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

    def _compile_lifecycle_plan(
        self,
        options: EphemeralTestRequest,
        *,
        bootstrap_lifecycles: Iterable[ExternallySatisfiedLifecycle],
    ) -> LifecyclePlan:
        """Compile and project all requested plans before cluster mutation."""

        requested = [(options.chart, options.profile)]
        if options.include_dependent_tests:
            requested.extend(
                (dependent.chart, dependent.profile)
                for dependent in self.resolver.dependent_tests(options.chart)
            )
        plans = [
            exclude_bootstrap_owned_charts(
                self.lifecycle_compiler.compile_cluster_test(
                    chart,
                    profile,
                    default_namespace=DEFAULT_NAMESPACE,
                    namespace_override=options.namespace,
                    lint=options.lint,
                ),
                bootstrap_lifecycles,
            )
            for chart, profile in requested
        ]
        return _merge_lifecycle_plans(plans)

def _new_lifecycle_run_id() -> str:
    """Mint one evidence-safe identity for an ephemeral cluster-test run."""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"cluster-test-{timestamp}-{uuid4().hex[:12]}"


def _merge_lifecycle_plans(plans: list[LifecyclePlan]) -> LifecyclePlan:
    """Compose authored fanout plans, executing each chart/profile action once.

    Action IDs include the profile, so the same release under different
    profiles is intentionally converged and tested once per profile. Identical
    chart/profile actions shared by multiple dependent plans are deduplicated
    while retaining first-authored plan order.
    """

    if not plans:
        raise ChartManagerError("cluster test produced no lifecycle plans")
    first = plans[0]
    actions: list[LifecycleAction] = []
    actions_by_id: dict[str, LifecycleAction] = {}
    warnings: list[str] = []
    for plan in plans:
        if plan.workflow is not first.workflow:
            raise ChartManagerError("cannot combine lifecycle plans from different workflows")
        for action in plan.actions:
            previous = actions_by_id.get(action.action_id)
            if previous is None:
                actions_by_id[action.action_id] = action
                actions.append(action)
            elif previous != action:
                raise ChartManagerError(
                    f"conflicting lifecycle action {action.action_id!r} across test plans"
                )
        for warning in plan.warnings:
            if warning not in warnings:
                warnings.append(warning)
    return replace(
        first,
        actions=tuple(actions),
        warnings=tuple(warnings),
    )


class _ExecutorHelmAdapter:
    """Narrow the production Helm API to the executor's structural port."""

    def __init__(self, helm: Helm) -> None:
        self.helm = helm

    def dependency_update_if_stale(self, chart_path: Path) -> object:
        return self.helm.dependency_update_if_stale(chart_path)

    def lint(self, chart_path: Path, values: list[Path] | None = None) -> None:
        self.helm.lint(chart_path, values or [])

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
