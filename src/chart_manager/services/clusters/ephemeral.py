"""Ephemeral chart testing on a locally configured Kubernetes cluster.

CI-shaped counterpart to ``DevelopmentClusterService``; see the development
cluster package docstring for the full contrast.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from chart_manager.api.local.v1alpha1 import LocalCluster
from chart_manager.domain.cluster_tests import ClusterTestCatalog
from chart_manager.domain.install_plan import DependencyResolver
from chart_manager.domain.local_resources import LocalResourceLoader
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.clusters._shared import kind_config_path
from chart_manager.services.clusters.bootstrap import LocalBootstrapExecutor
from chart_manager.services.clusters.environment import (
    ClientFactory,
    EnvironmentHandle,
    EnvironmentSpec,
    KindEnvironmentProvider,
    KubernetesEnvironmentProvider,
)
from chart_manager.services.lifecycle.cluster_executor import (
    ClusterActionExecutor,
    HelmTestResult,
)
from chart_manager.services.lifecycle.compiler import ClusterTestCompiler
from chart_manager.services.lifecycle.models import (
    ActionKind,
    LifecycleAction,
    LifecyclePlan,
)
from chart_manager.services.lifecycle.plan_projection import (
    ExternallySatisfiedLifecycle,
    exclude_bootstrap_owned_charts,
)
from chart_manager.services.progress import ProgressCallback, info, step, warn
from chart_manager.settings import DEFAULT_CHARTS_DIR, DEFAULT_LOCAL_CONFIG

#: Diagnostic channel. This service is the CI-shaped one, where the process
#: that failed is frequently no longer around to be asked and its terminal
#: narration is gone with it.
_LOG = logging.getLogger(__name__)

DEFAULT_CLUSTER_NAME = "chart-manager"

#: Namespace for a cluster-test profile that declares no `namespace:`, on the
#: ephemeral `chart test` path.
#:
#: Deliberately *not* `_shared.DEFAULT_NAMESPACE` ("default"), which is what
#: the bootstrap and `local up` paths use for the same authored gap. The fork
#: is real -- `charts/{grafana,loki,mimir-distributed,tempo,alloy,...}` all
#: omit `namespace:`, so those charts land in `observability` under
#: `chart test` and in `default` under `local up` -- and it is deliberate on
#: this side: this is the namespace the lab's LGTM stack is authored around,
#: and `cli/grafana.py` imports *this constant* as the default `--namespace`
#: for `grafana dashboard export`/`lint`, so a `chart test grafana` followed
#: by a dashboard export agrees without either command passing a flag.
#:
#: One caveat if either value is ever revisited: bootstrap publishes its
#: ownership identities under `_shared.DEFAULT_NAMESPACE`, and
#: `exclude_bootstrap_owned_charts` matches on the namespace, so a bootstrap
#: release whose profile omits `namespace:` would be installed here a second
#: time. Every LocalCluster bootstrap release in this repository declares one
#: (`charts/cilium` -> `kube-system`), which is why the fork has stayed
#: invisible.
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
        local_config: Path = DEFAULT_LOCAL_CONFIG,
        environment_provider: KubernetesEnvironmentProvider | None = None,
        client_factory: ClientFactory | None = None,
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
        self.cluster_test_compiler = ClusterTestCompiler(
            self.root,
            charts_dir=charts_dir,
            cluster_tests=self.cluster_tests,
            resolver=self.resolver,
        )
        self.helm = helm
        self.kind = kind
        self.kubectl = kubectl
        self.environment_provider = environment_provider or KindEnvironmentProvider(kind)
        self.local_resources = LocalResourceLoader(self.root, local_config=local_config)
        self._client_factory = client_factory
        # No-op default so the narration call sites don't need a None check.
        self._progress: ProgressCallback = progress or (lambda _event: None)

    def ensure_cluster(self, cluster_name: str = DEFAULT_CLUSTER_NAME) -> str:
        """Create or start the configured local cluster; return its name.

        ``LocalCluster`` owns the Kind config path and bootstrap sequence;
        callers only select the cluster identity.
        """
        self._ensure_environment(cluster_name, self.local_resources.load_cluster())
        return cluster_name

    def _environment_spec(
        self,
        cluster_name: str,
        local_cluster: LocalCluster,
    ) -> EnvironmentSpec:
        return EnvironmentSpec(
            name=cluster_name,
            cluster_name=cluster_name,
            config=kind_config_path(self.root, local_cluster),
        )

    def _ensure_environment(
        self,
        cluster_name: str,
        local_cluster: LocalCluster,
    ) -> EnvironmentHandle:
        """Create/start the environment and bind the clients to its context."""
        self._progress(step("Ensuring local cluster", cluster_name))
        return self._bind_clients(
            self.environment_provider.ensure(
                self._environment_spec(cluster_name, local_cluster)
            )
        )

    def _bind_clients(self, handle: EnvironmentHandle) -> EnvironmentHandle:
        """Address every cluster-facing client at the resolved environment.

        Returned rather than stored: the handle is what bootstrap converges
        against, and a caller that has not resolved one has nothing to
        converge -- keeping it a local makes that unrepresentable.
        """
        if self._client_factory is not None:
            # The factory also hands back an ExposeService; this service owns
            # no port-forward, so it simply does not read it.
            bound = self._client_factory(handle)
            self.helm, self.kubectl = bound.helm, bound.kubectl
        return handle

    def _bootstrap_executor(self) -> LocalBootstrapExecutor:
        """A bootstrap executor bound to the clients bound *right now*.

        Built per phase and never stored, for the reason
        `DevelopmentClusterService._bootstrap_executor` documents at length:
        `_bind_clients` rebinds `self.helm` / `self.kubectl` to the resolved
        kubecontext, and preflight has to run before the cluster is mutated
        while converge has to run after.
        """
        return LocalBootstrapExecutor(
            self.root,
            helm=self.helm,
            kind=self.kind,
            kubectl=self.kubectl,
            progress=self._progress,
        )

    def plan(self, options: EphemeralTestRequest) -> LifecyclePlan:
        """Compile what ``run`` would execute, without touching a cluster.

        The plan is the whole answer a `--dry-run` needs, and it is the same
        object `run` executes -- compiled by the same call, from the same
        authored intent -- so a printed plan cannot describe work the real
        run would not do.

        Linting is skipped whatever the request says. `preflight(lint=True)`
        shells out to `helm dependency update` and `helm lint`, which writes
        into `charts/*/charts/` and is exactly the kind of side effect a dry
        run promises not to have. Nothing about the compiled plan depends on
        it: `lint` only adds a HELM_LINT action, which the compiler derives
        from the request, not from the preflight.
        """
        _cluster, plan = self._load_and_compile(options, lint=False)
        return plan

    def _load_and_compile(
        self,
        options: EphemeralTestRequest,
        *,
        lint: bool,
    ) -> tuple[LocalCluster, LifecyclePlan]:
        """Load authored cluster config and compile the plan to execute."""
        # Bootstrap ownership is authored configuration, not process state.
        # Reload it for every run so a long-lived service cannot carry an
        # earlier run's externally-satisfied identities forward.
        local_cluster = self.local_resources.load_cluster()
        bootstrap_lifecycles = self._bootstrap_executor().preflight(
            local_cluster,
            lint=lint,
        )
        return local_cluster, self._compile_lifecycle_plan(
            options,
            bootstrap_lifecycles=bootstrap_lifecycles,
        )

    def run(self, options: EphemeralTestRequest) -> EphemeralTestResult:
        """Ensure the cluster, run configured bootstrap, install, and test.

        Fail-fast: the first chart error propagates (contrast
        ``DevelopmentClusterService.up``,
        which continues on error). `include_dependent_tests` re-runs the plans
        of charts declared as dependent-test targets. Returns the
        accounting of what was installed and tested; narration goes to the
        injected progress callback.
        """
        started = time.monotonic()
        local_cluster, plan = self._load_and_compile(options, lint=options.lint)
        _LOG.info(
            "chart test run started: chart=%s profile=%s cluster=%s namespace=%s "
            "actions=%d ensure_cluster=%s include_dependent_tests=%s lint=%s",
            options.chart,
            options.profile,
            options.cluster_name,
            options.namespace or DEFAULT_NAMESPACE,
            len(plan.actions),
            options.ensure_cluster,
            options.include_dependent_tests,
            options.lint,
        )
        if options.ensure_cluster:
            handle = self._ensure_environment(options.cluster_name, local_cluster)
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
            handle = self._bind_clients(
                self.environment_provider.handle(
                    self._environment_spec(options.cluster_name, local_cluster)
                )
            )

        installed: set[str] = set()
        tested: list[str] = []
        namespaces_created: set[str] = set()

        # Built here rather than reused from `_load_and_compile`: the clients
        # were rebound above, and an executor from the preflight phase would
        # converge bootstrap against the pre-rebind ones.
        bootstrap = self._bootstrap_executor()
        for outcome in bootstrap.execute(local_cluster, environment=handle):
            installed.add(outcome.name)
            namespaces_created.add(outcome.namespace)

        self._execute_lifecycle_plan(
            plan=plan,
            installed=installed,
            tested=tested,
            namespaces_created=namespaces_created,
        )

        # Only reached on success: `_execute_lifecycle_plan` raises on the first
        # failed action, and that path logs its own ERROR before raising.
        _LOG.info(
            "chart test run finished: chart=%s cluster=%s installed=%d tested=%d "
            "namespaces=%d elapsed=%.1fs",
            options.chart,
            options.cluster_name,
            len(installed),
            len(tested),
            len(namespaces_created),
            time.monotonic() - started,
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
            progress=self._progress,
        )
        result = executor.execute(plan)

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
        # Logged before the diagnostics attempt: collecting them is itself a
        # cluster round trip that can fail or hang, and the identity of the
        # action that failed must not depend on it succeeding.
        _LOG.error(
            "cluster action failed: chart=%s action=%s kind=%s namespace=%s: %s",
            failed_action.target.chart,
            failure.action_id,
            failed_action.kind.value,
            namespace or "(none)",
            failure.detail,
        )
        if namespace is not None:
            try:
                diagnostics = self.kubectl.diagnostics(namespace)
            except Exception as exc:
                _LOG.warning(
                    "namespace diagnostics unavailable for the failing action: "
                    "namespace=%s: %s: %s",
                    namespace,
                    type(exc).__name__,
                    exc,
                )
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
                self.cluster_test_compiler.compile_cluster_test(
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
