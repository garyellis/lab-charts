"""The target convergence engine and persistent-environment lifecycle.

Target convergence and the install loop share three hand-threaded mutable
accumulators (`RunSummary`, `installed_keys`, `namespaces_created`). Drift
detection and access hints, which need neither the accumulators nor the chart
repository, live in `drift.py` / `access.py`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from chart_manager.api.local.v1alpha1 import LifecycleRelease, OciChartRelease
from chart_manager.domain.cluster_tests import ClusterTestCatalog
from chart_manager.domain.install_plan import InstallPlanEntry
from chart_manager.domain.lifecycle_policy import require_cluster_test_profile
from chart_manager.domain.local_resources import (
    LocalResourceLoader,
    ResolvedChartTarget,
    ResolvedLocalTarget,
)
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.clusters._shared import (
    DEFAULT_NAMESPACE,
    kind_config_path,
    lifecycle_install_plan,
    oci_chart_ref,
    oci_identity,
)
from chart_manager.services.clusters.bootstrap import LocalBootstrapExecutor
from chart_manager.services.clusters.development.access import (
    access_hints,
    wait_apps_wildcard_ready,
)
from chart_manager.services.clusters.development.drift import (
    warn_on_port_mapping_drift,
)
from chart_manager.services.clusters.development.models import (
    DevelopmentClusterAccessHints,
    DevelopmentClusterActionResult,
    DevelopmentClusterEntryFailure,
    DevelopmentClusterEntryOutcome,
    DevelopmentClusterPlan,
    DevelopmentClusterPlanEntry,
    DevelopmentClusterResult,
    DevelopmentClusterStatus,
    RunSummary,
)
from chart_manager.services.clusters.development.status import cluster_status
from chart_manager.services.clusters.environment import (
    BoundClients,
    ClientFactory,
    EnvironmentHandle,
    EnvironmentSpec,
    KindEnvironmentProvider,
    KubernetesEnvironmentProvider,
)
from chart_manager.services.expose import ExposeService
from chart_manager.services.lifecycle.plan_projection import ExternallySatisfiedLifecycle
from chart_manager.services.progress import (
    ProgressCallback,
    detail,
    failure,
    info,
    step,
    warn,
)
from chart_manager.settings import DEFAULT_LOCAL_CONFIG

#: Diagnostic channel, parallel to `self._progress`. Every `failure(...)` /
#: `warn(...)` narration below records an outcome the converge then *continues
#: past*; the narration is optional and unlevelled, so the same fact is logged
#: here for whoever reads the run afterwards.
_LOG = logging.getLogger(__name__)

# cert-manager webhook deployment. Must be Available before the
# istio-gateway chart installs (its Certificate / ClusterIssuer CRs go
# through the webhook). Subchart's default name is `cert-manager-webhook`.
CERT_MANAGER_WEBHOOK_DEPLOYMENT = "cert-manager-webhook"
CERT_MANAGER_WEBHOOK_NAMESPACE = "cert-manager"
CERT_MANAGER_WEBHOOK_TIMEOUT = "120s"
CERT_MANAGER_CHART = "cert-manager"


@dataclass(frozen=True)
class _TargetLocalExecution:
    catalog: ClusterTestCatalog
    plan: tuple[InstallPlanEntry, ...]


#: One preflighted release, carrying whatever converging it needs.
#:
#: `_preflight_target` used to return executions index-aligned with the
#: releases it was given, `None` for every OCI entry, and both callers
#: re-zipped the two sequences under an `assert execution is not None`. The
#: alignment was an invariant across a function boundary with nothing but the
#: assert holding it -- and `python -O` deletes asserts. A lifecycle release
#: and its resolved plan are one value here, so there is no pairing left to
#: get wrong.
type _TargetStep = _TargetLocalExecution | OciChartRelease


class DevelopmentClusterService:
    """Converge a chart or LocalStack onto a persistent local environment."""

    def __init__(
        self,
        root: Path,
        *,
        helm: Helm,
        kind: Kind,
        kubectl: Kubectl,
        expose: ExposeService,
        progress: ProgressCallback | None = None,
        local_config: Path = DEFAULT_LOCAL_CONFIG,
        environment_provider: KubernetesEnvironmentProvider | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        """Wire integrations; every cluster-facing collaborator is required.

        These used to default to `or Helm()` / `or Kubectl()`, and the CLI
        constructed the service with none of them -- so `Settings.kube_context`
        was configured in the composition root and then discarded here. The
        composition root is now the only place these are built.
        """
        self.root = root.resolve()
        self.helm = helm
        self.kind = kind
        self.kubectl = kubectl
        # ExposeService is injected so lifecycle operations can stop any active
        # port-forward in the same boundary as the cluster lifecycle -- a
        # kubectl port-forward whose apiserver has just been stopped is
        # dead weight, and leaving the CLI handler to clean it up split
        # the lifecycle across two layers.
        self.expose = expose
        self.environment_provider = environment_provider or KindEnvironmentProvider(kind)
        self.local_resources = LocalResourceLoader(self.root, local_config=local_config)
        self._client_factory = client_factory
        # No-op default so the narration call sites don't need a None check.
        self._progress: ProgressCallback = progress or (lambda _event: None)

    def up_target(
        self,
        target: ResolvedLocalTarget,
        *,
        profile: str | None,
        cluster_name: str,
        skip_installed: bool = False,
    ) -> DevelopmentClusterResult:
        """Prepare LocalCluster, run bootstrap, then converge a chart or LocalStack.

        All LocalCluster and target resources are loaded and preflighted before
        Kind is mutated. Bootstrap is fail-fast; workload convergence retains
        the development-friendly continue-on-error accounting.
        """
        started = time.monotonic()
        local_cluster = self.local_resources.load_cluster()
        releases = self._target_releases(target, profile=profile)
        steps = self._preflight_target(
            releases,
            excluded_lifecycle_identities=self._bootstrap_executor().preflight(local_cluster),
        )
        config = kind_config_path(self.root, local_cluster)
        _LOG.info(
            "local converge started: cluster=%s target=%s kind=%s profile=%s "
            "steps=%d skip_installed=%s",
            cluster_name,
            target.name,
            target.kind,
            profile or "(chart default)",
            len(steps),
            skip_installed,
        )
        self._progress(step("Ensuring local cluster", cluster_name))
        environment = self._ensure_environment(cluster_name, config=config)
        self._progress(step("Waiting for kube-apiserver"))
        self.kubectl.wait_apiserver_ready()

        summary = RunSummary()
        installed_keys = self._existing_release_keys()
        namespaces_created: set[str] = set()
        # Built here, after `_ensure_environment` rebound the clients: an
        # executor constructed alongside the preflight one above carries the
        # pre-rebind Helm and converges bootstrap against whatever
        # kubecontext the workstation happens to hold.
        bootstrap = self._bootstrap_executor()
        for outcome in bootstrap.execute(local_cluster, environment=environment):
            bucket = summary.applied if outcome.status == "applied" else summary.no_change
            bucket.append(
                DevelopmentClusterEntryOutcome(
                    outcome.name,
                    outcome.profile,
                    outcome.namespace,
                )
            )
            installed_keys.add((outcome.namespace, outcome.name))
            namespaces_created.add(outcome.namespace)

        for target_step in steps:
            if isinstance(target_step, _TargetLocalExecution):
                self._install_plan(
                    list(target_step.plan),
                    default_namespace=DEFAULT_NAMESPACE,
                    installed_keys=installed_keys,
                    namespaces_created=namespaces_created,
                    summary=summary,
                    skip_installed=skip_installed,
                    cluster_tests=target_step.catalog,
                )
                continue

            self._converge_oci_release(
                target_step.name,
                target_step,
                namespace=target_step.namespace,
                values=[self.root / path for path in target_step.values],
                timeout=target_step.timeout,
                installed_keys=installed_keys,
                summary=summary,
                skip_installed=skip_installed,
            )

        self._wait_apps_wildcard_ready(summary)
        self._warn_on_port_mapping_drift(cluster_name, config=config)
        fallback_namespace = next(
            (
                entry.namespace
                for entry in (*summary.applied, *summary.no_change)
                if entry.chart != local_cluster.metadata.name
            ),
            DEFAULT_NAMESPACE,
        )
        # `failed` is a count, not a raise: this path is continue-on-error, so
        # the run's exit status alone does not say how much of it converged.
        _LOG.info(
            "local converge finished: cluster=%s applied=%d no_change=%d failed=%d "
            "elapsed=%.1fs",
            cluster_name,
            len(summary.applied),
            len(summary.no_change),
            len(summary.failed),
            time.monotonic() - started,
        )
        return summary.freeze(self._access_hints(summary, namespace=fallback_namespace))

    def status(self, cluster_name: str) -> DevelopmentClusterStatus:
        """Report the current state of the development cluster.

        Read-only and total: nothing here mutates, and a cluster that is
        absent or unreachable is reported rather than raised. The kind
        config is resolved from the LocalCluster when there is one, so the
        drift check compares against the file `up` would have used and not
        against a same-named default that may not exist.

        The client factory is the same one `_ensure_environment` applies
        after creating the cluster, so a report and a converge address one
        kubecontext. Without it `status` answers about the workstation's
        ambient kubeconfig, which is a different cluster with the same
        command spelling.
        """
        return cluster_status(
            cluster_name,
            clients=self._client_factory or self._current_clients,
            kind=self.kind,
            environment_provider=self.environment_provider,
            root=self.root,
            config=self._authored_kind_config(),
        )

    def _current_clients(self, _handle: EnvironmentHandle) -> BoundClients:
        """Fall back to the injected clients when no factory was wired.

        Only tests construct the service without a factory; the composition
        root always supplies one.
        """
        return BoundClients(helm=self.helm, kubectl=self.kubectl, expose=self.expose)

    def _authored_kind_config(self) -> Path | None:
        """The LocalCluster's kind config, or None when it cannot be read.

        `status` must answer even in a repository with no authored
        LocalCluster (or an invalid one) -- that is a spec error worth
        raising from `up`, where it blocks a mutation, and worth nothing
        from a report, where it only means the drift check has no baseline.
        """
        try:
            local_cluster = self.local_resources.load_cluster()
        except (ChartManagerError, OSError) as exc:
            # Swallowed on purpose (see above), but not silently: with no
            # authored config the drift check downstream compares against a
            # default path that may not exist, and a typo'd `spec.cluster.config`
            # would otherwise disable that check permanently with no signal.
            _LOG.warning(
                "LocalCluster unreadable; port-mapping drift has no baseline: %s: %s",
                type(exc).__name__,
                exc,
            )
            return None
        return kind_config_path(self.root, local_cluster)

    def plan_target(
        self,
        target: ResolvedLocalTarget,
        *,
        profile: str | None,
        cluster_name: str,
        destroys: bool = False,
    ) -> DevelopmentClusterPlan:
        """Resolve what a converge would install, without touching the cluster.

        This is `up_target`'s preflight and nothing else: the same
        `load_cluster` / `bootstrap.preflight` / `_preflight_target`
        sequence, stopping where the mutating path calls `_ensure_environment`.
        A `--dry-run` therefore fails on an unresolvable plan exactly as the
        real run would, and succeeds only where the real run would proceed.

        Deliberately offline. Nothing here asks Kind, Helm, or the apiserver
        anything, so a plan can be printed with no cluster running and no
        Docker daemon -- which is most of what makes a dry run worth having.

        Bootstrap entries are reported sorted rather than in authored order:
        `LocalBootstrapExecutor.preflight` returns identities as a set, and
        re-deriving the sequence here would be a second copy of the
        bootstrap ordering rule for a display detail.
        """
        local_cluster = self.local_resources.load_cluster()
        bootstrap_identities = self._bootstrap_executor().preflight(local_cluster)
        steps = self._preflight_target(
            self._target_releases(target, profile=profile),
            excluded_lifecycle_identities=bootstrap_identities,
        )
        entries = [
            DevelopmentClusterPlanEntry(
                chart=identity.chart,
                profile=identity.profile,
                namespace=identity.namespace,
                source="bootstrap",
            )
            for identity in sorted(
                bootstrap_identities,
                key=lambda i: (i.chart, i.profile, i.namespace),
            )
        ]
        for target_step in steps:
            if isinstance(target_step, OciChartRelease):
                entries.append(
                    DevelopmentClusterPlanEntry(
                        chart=target_step.name,
                        profile=oci_identity(target_step),
                        namespace=target_step.namespace,
                        source="target",
                    )
                )
                continue
            for entry in target_step.plan:
                chart = target_step.catalog.get(entry.chart)
                entries.append(
                    DevelopmentClusterPlanEntry(
                        chart=entry.chart,
                        profile=entry.profile,
                        namespace=require_cluster_test_profile(
                            chart.spec, entry.profile
                        ).namespace
                        or DEFAULT_NAMESPACE,
                        source="target",
                    )
                )
        return DevelopmentClusterPlan(
            command="reset" if destroys else "up",
            cluster_name=cluster_name,
            target=target.name,
            target_kind=target.kind,
            destroys=destroys,
            entries=tuple(entries),
        )

    def plan_down(self, cluster_name: str) -> DevelopmentClusterPlan:
        """The plan for `down`: stop this cluster, install nothing.

        Offline for the same reason as `plan_target`. `down` takes no target
        and resolves no releases, so the whole plan is which cluster it
        addresses -- which is exactly the thing worth confirming before
        stopping it.
        """
        return DevelopmentClusterPlan(command="down", cluster_name=cluster_name)

    def down(self, cluster_name: str) -> DevelopmentClusterActionResult:
        """Stop the cluster's node containers; preserve all state.

        The provider preserves etcd, installed Helm releases, PVCs, and its
        image cache. A subsequent `up` converges the target again through
        `helm upgrade --install`; use `--skip-installed` to bypass releases
        already reported by Helm.

        Also stops any active access port-forward for this cluster. A kubectl
        port-forward whose apiserver has just stopped will exit on its own,
        but its recorded process state still needs to be reaped.
        """
        self._progress(step("Stopping local cluster", cluster_name))
        stopped = self.environment_provider.stop(self._handle(cluster_name))
        _LOG.info("local cluster stopped: cluster=%s changed=%s", cluster_name, stopped)
        return DevelopmentClusterActionResult(
            cluster_name=cluster_name,
            changed=stopped,
            port_forward_pid=self.expose.stop(cluster_name),
        )

    def _destroy_environment(self, cluster_name: str) -> DevelopmentClusterActionResult:
        """Ask the selected provider to destroy the environment entirely.

        Destructive: image cache, etcd, and any data in node-local PVs are
        gone. Use `down` if you want a fast restart. Any active port-forward
        is stopped for the same reason as `down`.
        """
        self._progress(step("Deleting local cluster", cluster_name))
        deleted = self.environment_provider.destroy(self._handle(cluster_name))
        _LOG.info("local cluster destroyed: cluster=%s changed=%s", cluster_name, deleted)
        return DevelopmentClusterActionResult(
            cluster_name=cluster_name,
            changed=deleted,
            port_forward_pid=self.expose.stop(cluster_name),
        )

    def reset_target(
        self,
        target: ResolvedLocalTarget,
        *,
        profile: str | None,
        cluster_name: str,
    ) -> DevelopmentClusterResult:
        """Destroy and fully converge a chart or LocalStack."""
        # All authored state is resolved before deleting a healthy cluster.
        local_cluster = self.local_resources.load_cluster()
        bootstrap_identities = self._bootstrap_executor().preflight(local_cluster)
        self._preflight_target(
            self._target_releases(target, profile=profile),
            excluded_lifecycle_identities=bootstrap_identities,
        )
        self._destroy_environment(cluster_name)
        return self.up_target(
            target,
            profile=profile,
            cluster_name=cluster_name,
            skip_installed=False,
        )

    def _bootstrap_executor(self) -> LocalBootstrapExecutor:
        """A bootstrap executor bound to the clients bound *right now*.

        Built per phase and never stored: `_ensure_environment` rebinds
        `self.helm` / `self.kubectl` to the resolved kubecontext, so an
        executor that outlives that call converges against the clients it
        was constructed with. The preflight phase has to run before the
        cluster is mutated and the converge phase has to run after, which is
        exactly why one object cannot serve both.
        """
        return LocalBootstrapExecutor(
            self.root,
            helm=self.helm,
            kind=self.kind,
            kubectl=self.kubectl,
            progress=self._progress,
        )

    def _handle(self, cluster_name: str) -> EnvironmentHandle:
        """Build the provider-owned stable identity for a lifecycle operation."""
        return self.environment_provider.handle(
            EnvironmentSpec(
                name=cluster_name,
                cluster_name=cluster_name,
            )
        )

    def _ensure_environment(
        self,
        cluster_name: str,
        *,
        config: Path | None = None,
    ) -> EnvironmentHandle:
        spec = EnvironmentSpec(
            name=cluster_name,
            cluster_name=cluster_name,
            config=config,
        )
        handle = self.environment_provider.ensure(spec)
        if self._client_factory is not None:
            bound = self._client_factory(handle)
            self.helm, self.kubectl, self.expose = bound.helm, bound.kubectl, bound.expose
        return handle

    def _target_releases(
        self,
        target: ResolvedLocalTarget,
        *,
        profile: str | None,
    ) -> tuple[LifecycleRelease | OciChartRelease, ...]:
        if isinstance(target, ResolvedChartTarget):
            return (
                LifecycleRelease(
                    type="lifecycle",
                    chart=target.path.relative_to(self.root),
                    profile=profile or "minimal",
                ),
            )
        if profile is not None:
            raise ChartManagerError(
                "--profile is only valid for a chart target; LocalStack releases "
                "declare their profiles"
            )
        return tuple(target.stack.spec.releases)

    def _preflight_target(
        self,
        releases: tuple[LifecycleRelease | OciChartRelease, ...],
        *,
        excluded_lifecycle_identities: frozenset[
            ExternallySatisfiedLifecycle
        ] = frozenset(),
    ) -> tuple[_TargetStep, ...]:
        """Compile and validate all local identities without mutating Helm state.

        Authored order is preserved: an OCI release passes through as itself
        (there is nothing to compile), a lifecycle release is replaced by the
        plan it resolved to.
        """
        seen: dict[Path, tuple[str, str]] = {}
        steps: list[_TargetStep] = []
        for release in releases:
            if isinstance(release, OciChartRelease):
                steps.append(release)
                continue
            catalog, plan = lifecycle_install_plan(
                self.root, release, source="local release"
            )
            deduped: list[InstallPlanEntry] = []
            for entry in plan:
                chart = catalog.get(entry.chart)
                chart_path = chart.path.resolve()
                entry_profile = require_cluster_test_profile(chart.spec, entry.profile)
                effective_namespace = entry_profile.namespace or DEFAULT_NAMESPACE
                external_identity = ExternallySatisfiedLifecycle(
                    chart_path=chart_path,
                    chart=entry.chart,
                    profile=entry.profile,
                    namespace=effective_namespace,
                )
                if external_identity in excluded_lifecycle_identities:
                    continue
                identity = (entry.profile, effective_namespace)
                previous = seen.get(chart_path)
                if previous == identity:
                    continue
                if previous is not None:
                    raise ChartManagerError(
                        f"conflicting local lifecycle identities for {entry.chart}: "
                        f"first {previous[0]} in {previous[1]}, then "
                        f"{entry.profile} in {effective_namespace}"
                    )
                seen[chart_path] = identity
                deduped.append(entry)
            steps.append(
                _TargetLocalExecution(
                    catalog=catalog,
                    plan=tuple(deduped),
                )
            )
        return tuple(steps)

    # ----- internals --------------------------------------------------------

    def _existing_release_keys(self) -> set[tuple[str, str]]:
        """Snapshot of (namespace, release-name) pairs already installed.

        Used to skip charts on re-run. Best-effort: a failure to list (no
        kubeconfig, cluster just created and apiserver still settling, etc.)
        falls back to "nothing installed" rather than aborting.

        Catches the base error rather than `ExternalCommandError` alone, so
        this agrees with `status._releases`, which asks Helm the same
        question about the same cluster.
        """
        try:
            releases = self.helm.list_releases(all_namespaces=True)
        except ChartManagerError as exc:
            _LOG.warning(
                "helm release listing failed; treating every release as uninstalled: %s",
                exc,
            )
            self._progress(
                warn(f"could not list helm releases ({exc}); proceeding as if no releases exist")
            )
            return set()
        return {(r.namespace, r.name) for r in releases}

    def _install_plan(
        self,
        plan: list[InstallPlanEntry],
        *,
        default_namespace: str,
        installed_keys: set[tuple[str, str]],
        namespaces_created: set[str],
        summary: RunSummary,
        skip_installed: bool,
        cluster_tests: ClusterTestCatalog,
    ) -> None:
        """Converge each plan entry, bucketing outcomes into `summary`.

        The catalog is required and always derived from the release's own
        chart path by the caller -- a repository-wide default here would
        silently resolve charts against the wrong tree.

        Continue-on-error: a failed entry is recorded and the loop moves
        on. Mutates `installed_keys` / `namespaces_created` in place.
        """
        catalog = cluster_tests
        for entry in plan:
            try:
                chart = catalog.get(entry.chart)
            except ChartManagerError as exc:
                _LOG.error(
                    "chart resolution failed; recorded as a failed row: chart=%s profile=%s: %s",
                    entry.chart,
                    entry.profile,
                    exc,
                )
                self._progress(failure("chart resolution failed:", f"{entry.chart}: {exc}"))
                summary.failed.append(
                    DevelopmentClusterEntryFailure(
                        chart=entry.chart,
                        profile=entry.profile,
                        namespace="?",
                        error=str(exc),
                    )
                )
                continue

            # Profile lookup is inside the guard too.
            # `require_cluster_test_profile()` raises
            # SpecError for an unknown name, and sitting outside every try it
            # aborted the entire plan -- so one chart whose `requires:` named
            # a renamed profile took down an 18-chart converge instead of
            # being recorded as a single failed row, contradicting the
            # continue-on-error contract this method documents.
            try:
                profile = require_cluster_test_profile(chart.spec, entry.profile)
            except ChartManagerError as exc:
                _LOG.error(
                    "profile resolution failed; recorded as a failed row: "
                    "chart=%s profile=%s: %s",
                    entry.chart,
                    entry.profile,
                    exc,
                )
                self._progress(failure("profile resolution failed:", f"{entry.chart}: {exc}"))
                summary.failed.append(
                    DevelopmentClusterEntryFailure(
                        chart=entry.chart,
                        profile=entry.profile,
                        namespace="?",
                        error=str(exc),
                    )
                )
                continue

            release = entry.chart
            namespace = profile.namespace or default_namespace
            key = (namespace, release)

            # Fast-skip path: opt-in only via `--skip-installed`. Default
            # behavior is to converge so values-file edits are picked up
            # on re-run (the helmfile/Argo workflow). Helm itself no-ops
            # when nothing rendered differently -- the revision-compare
            # in upgrade_install reports that as no-change without us
            # needing to short-circuit.
            if skip_installed and key in installed_keys:
                self._progress(
                    detail(
                        "skip",
                        f"{entry.chart}:{entry.profile} (already installed in {namespace})",
                    )
                )
                summary.no_change.append(
                    DevelopmentClusterEntryOutcome(
                        entry.chart,
                        entry.profile,
                        namespace,
                    )
                )
                namespaces_created.add(namespace)
                continue

            try:
                # Namespace creation is inside the guard for the same reason
                # profile resolution above is: `kubectl.create_namespace`
                # tolerates an "already exists" exit, but a CommandTimeout or
                # a missing binary still raises, and sitting outside the try
                # that aborted the whole converge instead of recording one
                # failed row.
                if namespace not in namespaces_created:
                    self.kubectl.create_namespace(namespace)
                    namespaces_created.add(namespace)
                values = catalog.value_paths(chart, entry.profile)
                self._progress(step("Updating dependencies", entry.chart))
                # mtime-gated: skips the subprocess when Chart.lock is
                # already newer than Chart.yaml and charts/ is populated.
                # Per-chart per-process cache prevents repeat fetches.
                self.helm.dependency_update_if_stale(chart.path)
                self._progress(step("Applying", f"{entry.chart}:{entry.profile} -> {namespace}"))
                with self._diagnostics_on_failure(namespace):
                    # wait=False is load-bearing: see issues.md #2. Several
                    # charts in the plan (loki, mimir) deadlock under --wait
                    # because their post-install hooks bootstrap the very
                    # buckets the main pods need to become Ready.
                    result = self.helm.upgrade_install(
                        release,
                        chart.path,
                        namespace=namespace,
                        values=values,
                        timeout=profile.timeout,
                        wait=False,
                    )
                # Single source of truth for the "did helm produce a new
                # revision?" decision. Used both for the rollout-wait gate
                # and for the summary bucket classification below; binding
                # once keeps the two callsites from drifting.
                applied = result.status == "applied"
                if applied:
                    # New revision => something actually changed; wait for
                    # rollouts so the dev sees the new state ready, and so
                    # subsequent charts that may depend on these workloads
                    # aren't racing against a still-rolling deployment.
                    self._progress(step("Waiting for workloads", entry.chart))
                    self.kubectl.wait_workloads_ready(namespace, timeout=profile.timeout)
                    self._post_install_hook(entry.chart)
                else:
                    # No-change: nothing is rolling, so the rollout-status
                    # wait would just be a no-op against the existing
                    # generation. Skipping it is the biggest single time
                    # savings on a converge-with-no-edits re-run. Print a
                    # dim marker so the skip is observable in the run log.
                    self._progress(detail("no change", f"{entry.chart} (rollout wait skipped)"))
                bucket = summary.applied if applied else summary.no_change
                bucket.append(DevelopmentClusterEntryOutcome(entry.chart, entry.profile, namespace))
                installed_keys.add(key)
            except ChartManagerError as exc:
                _LOG.error(
                    "chart apply failed; converge continues: chart=%s profile=%s "
                    "namespace=%s: %s",
                    entry.chart,
                    entry.profile,
                    namespace,
                    exc,
                )
                self._progress(failure("apply failed:", f"{entry.chart}:{entry.profile} -> {exc}"))
                summary.failed.append(
                    DevelopmentClusterEntryFailure(
                        chart=entry.chart,
                        profile=entry.profile,
                        namespace=namespace,
                        error=str(exc),
                    )
                )
                continue

    def _converge_oci_release(
        self,
        release: str,
        source: OciChartRelease,
        *,
        namespace: str,
        values: list[Path],
        timeout: str,
        installed_keys: set[tuple[str, str]],
        summary: RunSummary,
        skip_installed: bool,
    ) -> None:
        """Converge one immutable OCI Helm source with Helm-owned readiness."""
        key = (namespace, release)
        identity = oci_identity(source)
        if key in installed_keys and skip_installed:
            self._progress(detail("skip", f"{release} (already installed in {namespace})"))
            summary.no_change.append(
                DevelopmentClusterEntryOutcome(release, identity, namespace)
            )
            return

        missing_values = [path for path in values if not path.is_file()]
        if missing_values:
            message = "OCI values file(s) not found: " + ", ".join(map(str, missing_values))
            _LOG.error(
                "OCI release skipped; converge continues: release=%s identity=%s "
                "namespace=%s: %s",
                release,
                identity,
                namespace,
                message,
            )
            self._progress(failure("apply failed:", f"{release} -> {message}"))
            summary.failed.append(
                DevelopmentClusterEntryFailure(
                    chart=release,
                    profile=identity,
                    namespace=namespace,
                    error=message,
                )
            )
            return

        try:
            self._progress(step("Converging OCI release", f"{release}@{identity}"))
            result = self.helm.upgrade_install(
                release,
                oci_chart_ref(source),
                namespace=namespace,
                values=values,
                timeout=timeout,
                wait=True,
                version=source.version,
            )
            bucket = summary.applied if result.status == "applied" else summary.no_change
            bucket.append(DevelopmentClusterEntryOutcome(release, identity, namespace))
            installed_keys.add(key)
        except ChartManagerError as exc:
            _LOG.error(
                "OCI release apply failed; converge continues: release=%s identity=%s "
                "namespace=%s: %s",
                release,
                identity,
                namespace,
                exc,
            )
            self._progress(failure("apply failed:", f"{release}@{identity} -> {exc}"))
            summary.failed.append(
                DevelopmentClusterEntryFailure(
                    chart=release,
                    profile=identity,
                    namespace=namespace,
                    error=str(exc),
                )
            )

    def _post_install_hook(self, chart: str) -> None:
        """Best-effort follow-up wait after the chart's own rollout-ready.

        Single hook today: after cert-manager applies, wait for the webhook
        Deployment's Available condition before letting the loop advance to
        istio-gateway (which submits Certificate / ClusterIssuer CRs through
        that webhook). This is the place to add more per-chart hooks if a
        second one is ever needed; while there's only one, keep it inline
        rather than a dispatch table -- a `if chart == X` is grep-able and
        the table indirection earns nothing for a single entry.

        If the wait fails (e.g. webhook Deployment never becomes Available),
        we warn and continue: it's better to surface the chart's downstream
        admission failure on the next install than to block here on a
        webhook race. The dev gate is best-effort by design.
        """
        if chart == CERT_MANAGER_CHART:
            self._progress(
                step(
                    "Waiting for",
                    f"Deployment/{CERT_MANAGER_WEBHOOK_DEPLOYMENT} "
                    f"-n {CERT_MANAGER_WEBHOOK_NAMESPACE}",
                )
            )
            try:
                self.kubectl.wait_deployment_available(
                    CERT_MANAGER_WEBHOOK_DEPLOYMENT,
                    namespace=CERT_MANAGER_WEBHOOK_NAMESPACE,
                    timeout=CERT_MANAGER_WEBHOOK_TIMEOUT,
                )
            except ChartManagerError as exc:
                _LOG.warning(
                    "cert-manager webhook not Available; later CR submissions may fail: "
                    "deployment=%s namespace=%s: %s",
                    CERT_MANAGER_WEBHOOK_DEPLOYMENT,
                    CERT_MANAGER_WEBHOOK_NAMESPACE,
                    exc,
                )
                self._progress(
                    warn(
                        f"cert-manager webhook not Available "
                        f"({exc}); subsequent CR submissions may fail"
                    )
                )

    @contextmanager
    def _diagnostics_on_failure(self, namespace: str) -> Iterator[None]:
        """Emit namespace diagnostics on subprocess failure, then re-raise."""
        # Mirror EphemeralTestClusterService's pattern: dump pods+events on subprocess
        # failure, then re-raise so the install loop's try/except records
        # the failure and moves on.
        try:
            yield
        except ExternalCommandError:
            diagnostics = self.kubectl.diagnostics(namespace)
            if diagnostics.strip():
                self._progress(info(diagnostics))
            raise

    # ----- bindings to the collaborator-scoped helpers -----------------------
    #
    # These four are one-line adapters: they bind `self`'s collaborators to
    # the free functions in `access.py` / `drift.py`. Keeping them as methods
    # is what lets those modules stay free of `DevelopmentClusterService` while the converge
    # engine reads the same as it did before the split.

    def _wait_apps_wildcard_ready(self, summary: RunSummary) -> None:
        """Wait for the wildcard cert, best-effort (see access.py)."""
        wait_apps_wildcard_ready(summary, kubectl=self.kubectl, progress=self._progress)

    def _access_hints(
        self, summary: RunSummary, *, namespace: str
    ) -> DevelopmentClusterAccessHints:
        """Resolve the post-converge advisory data (see access.py)."""
        return access_hints(summary, kubectl=self.kubectl, namespace=namespace)

    def _warn_on_port_mapping_drift(
        self,
        cluster_name: str,
        *,
        config: Path | None = None,
    ) -> None:
        """Warn on kind-config host-port drift (see drift.py)."""
        warn_on_port_mapping_drift(
            cluster_name,
            kind=self.kind,
            root=self.root,
            progress=self._progress,
            config=config,
        )
