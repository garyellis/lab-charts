"""The target convergence engine and persistent-environment lifecycle.

Target convergence and the install loop share two hand-threaded mutable
accumulators (`RunSummary`, `installed_keys`). Drift
detection and access hints, which need neither the accumulators nor the chart
repository, live in `drift.py` / `access.py`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from chart_manager.api.v1alpha1.local_cluster import LocalCluster
from chart_manager.api.v1alpha1.releases import (
    LifecycleRelease,
    OciChartRelease,
    RepoChartRelease,
)
from chart_manager.domain.local_resources import (
    ResolvedLocalTarget,
)
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.commands import CommandRunner, SubprocessRunner
from chart_manager.plumbing.errors import ChartManagerError
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
from chart_manager.services.clusters.provisioning_hooks import ProvisioningHookRunner
from chart_manager.services.expose import ExposeService
from chart_manager.shared.charts.chart import ResolvedChartTarget
from chart_manager.shared.charts.cluster_tests import ClusterTestCatalog
from chart_manager.shared.charts.install_plan import InstallPlanEntry
from chart_manager.shared.charts.lifecycle import require_cluster_test_profile
from chart_manager.shared.cluster import bootstrap
from chart_manager.shared.cluster.bootstrap import ExternallySatisfiedLifecycle
from chart_manager.shared.cluster.converge import Release, ReleaseFailed, converge, installed
from chart_manager.shared.cluster.local_cluster import load_cluster
from chart_manager.shared.cluster.progress import (
    ProgressCallback,
    detail,
    failure,
    info,
    step,
    warn,
)
from chart_manager.shared.cluster.releases import (
    lifecycle_install_plan,
    oci_chart_ref,
    oci_identity,
)
from chart_manager.shared.cluster.session import Session, kind_config_path
from chart_manager.shared.workspace import RepositoryWorkspace

#: Diagnostic channel, parallel to `self._progress`. Every `failure(...)` /
#: `warn(...)` narration below records an outcome the converge then *continues
#: past*; the narration is optional and unlevelled, so the same fact is logged
#: here for whoever reads the run afterwards.
_LOG = logging.getLogger(__name__)



@dataclass(frozen=True)
class _TargetLocalExecution:
    catalog: ClusterTestCatalog
    plan: tuple[InstallPlanEntry, ...]


#: One preflighted release, carrying whatever converging it needs.
#:
#: `_preflight_target` used to return executions index-aligned with the
#: releases it was given, `None` for every remote entry, and both callers
#: re-zipped the two sequences under an `assert execution is not None`. The
#: alignment was an invariant across a function boundary with nothing but the
#: assert holding it -- and `python -O` deletes asserts. A lifecycle release
#: and its resolved plan are one value here, so there is no pairing left to
#: get wrong.
type _TargetStep = _TargetLocalExecution | OciChartRelease | RepoChartRelease


@dataclass(frozen=True)
class _PreparedConverge:
    local_cluster: LocalCluster
    steps: tuple[_TargetStep, ...]
    config: Path


class DevelopmentClusterService:
    """Converge a chart or LocalStack onto a persistent local environment."""

    def __init__(
        self,
        *,
        workspace: RepositoryWorkspace,
        helm: Helm,
        kind: Kind,
        kubectl: Kubectl,
        expose: ExposeService,
        progress: ProgressCallback | None = None,
        environment_provider: KubernetesEnvironmentProvider | None = None,
        client_factory: ClientFactory | None = None,
        command_runner: CommandRunner | None = None,
        command_timeout: float | None = None,
    ) -> None:
        """Wire integrations; every cluster-facing collaborator is required.

        These used to default to `or Helm()` / `or Kubectl()`, and the CLI
        constructed the service with none of them -- so `Settings.kube_context`
        was configured in the composition root and then discarded here. The
        composition root is now the only place these are built.
        """
        self.root = workspace.root
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
        self._workspace = workspace
        self._client_factory = client_factory
        self._hooks = ProvisioningHookRunner(
            self.root,
            runner=command_runner or SubprocessRunner(),
            timeout=command_timeout,
        )
        self.run_provision_hooks = True
        # No-op default so the narration call sites don't need a None check.
        self._progress: ProgressCallback = progress or (lambda _event: None)

    def up_target(
        self,
        target: ResolvedLocalTarget,
        *,
        profile: str | None,
        cluster_name: str,
        skip_installed: bool = False,
        run_provision_hooks: bool | None = None,
    ) -> DevelopmentClusterResult:
        """Prepare LocalCluster, run bootstrap, then converge a chart or LocalStack.

        All LocalCluster and target resources are loaded and preflighted before
        Kind is mutated. Bootstrap is fail-fast; workload convergence retains
        the development-friendly continue-on-error accounting.
        """
        started = time.monotonic()
        prepared = self._prepare_target(target, profile=profile)
        return self._converge_prepared(
            prepared,
            target=target,
            profile=profile,
            cluster_name=cluster_name,
            skip_installed=skip_installed,
            run_provision_hooks=(
                self.run_provision_hooks if run_provision_hooks is None else run_provision_hooks
            ),
            run_pre_hook=True,
            started=started,
        )

    def _converge_prepared(
        self,
        prepared: _PreparedConverge,
        *,
        target: ResolvedLocalTarget,
        profile: str | None,
        cluster_name: str,
        skip_installed: bool,
        run_provision_hooks: bool,
        run_pre_hook: bool,
        started: float,
    ) -> DevelopmentClusterResult:
        local_cluster = prepared.local_cluster
        steps = prepared.steps
        config = prepared.config
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
        if run_provision_hooks and run_pre_hook:
            self._hooks.run("preProvision", local_cluster, cluster_name=cluster_name)
        self._progress(step("Ensuring local cluster", cluster_name))
        environment = self._ensure_environment(cluster_name, config=config)
        self._progress(step("Waiting for kube-apiserver"))
        self.kubectl.wait_apiserver_ready()
        if run_provision_hooks:
            self._hooks.run(
                "postProvision",
                local_cluster,
                cluster_name=cluster_name,
                environment=environment,
            )
            self._progress(step("Waiting for kube-apiserver after post-provision hook"))
            self.kubectl.wait_apiserver_ready()

        summary = RunSummary()
        lab = Session(
            name=cluster_name,
            context=environment.context,
            kind=self.kind,
            helm=self.helm,
            kubectl=self.kubectl,
        )
        installed_keys = self._existing_release_keys(lab)
        for outcome in bootstrap.bootstrap(
            lab, local_cluster, root=self.root, progress=self._progress
        ):
            bucket = summary.applied if outcome.status == "applied" else summary.no_change
            bucket.append(
                DevelopmentClusterEntryOutcome(outcome.name, outcome.profile, outcome.namespace)
            )
            installed_keys.add((outcome.namespace, outcome.name))

        for target_step in steps:
            if isinstance(target_step, _TargetLocalExecution):
                self._install_plan(
                    lab,
                    list(target_step.plan),
                    installed_keys=installed_keys,
                    summary=summary,
                    skip_installed=skip_installed,
                    cluster_tests=target_step.catalog,
                )
                continue
            release, profile = self._stack_release(target_step)
            self._converge_release(
                lab,
                release,
                profile,
                installed_keys=installed_keys,
                summary=summary,
                skip_installed=skip_installed,
            )

        self._wait_apps_wildcard_ready(summary)
        self._warn_on_port_mapping_drift(cluster_name, config=config)
        # `failed` is a count, not a raise: this path is continue-on-error, so
        # the run's exit status alone does not say how much of it converged.
        _LOG.info(
            "local converge finished: cluster=%s applied=%d no_change=%d failed=%d elapsed=%.1fs",
            cluster_name,
            len(summary.applied),
            len(summary.no_change),
            len(summary.failed),
            time.monotonic() - started,
        )
        return summary.freeze(self._access_hints(summary))

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
            local_cluster = load_cluster(self._workspace)
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
        run_provision_hooks: bool | None = None,
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
        local_cluster = load_cluster(self._workspace)
        bootstrap_identities = bootstrap.preflight(local_cluster, root=self.root)
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
            if isinstance(target_step, RepoChartRelease):
                entries.append(
                    DevelopmentClusterPlanEntry(
                        chart=target_step.name,
                        profile=target_step.version,
                        namespace=target_step.namespace,
                        source="target",
                    )
                )
                continue
            if not isinstance(target_step, _TargetLocalExecution):
                raise ChartManagerError(f"unsupported local target step: {target_step!r}")
            for entry in target_step.plan:
                chart = target_step.catalog.get(entry.chart)
                entries.append(
                    DevelopmentClusterPlanEntry(
                        chart=entry.chart,
                        profile=entry.profile,
                        namespace=require_cluster_test_profile(chart.spec, entry.profile).namespace,
                        source="target",
                    )
                )
        hooks_enabled = (
            self.run_provision_hooks if run_provision_hooks is None else run_provision_hooks
        )
        return DevelopmentClusterPlan(
            command="reset" if destroys else "up",
            cluster_name=cluster_name,
            target=target.name,
            target_kind=target.kind,
            destroys=destroys,
            entries=tuple(entries),
            provisioning_hooks_enabled=hooks_enabled,
            provisioning_hooks=(
                ()
                if local_cluster.spec.cluster.hooks is None
                else tuple(
                    (phase, tuple(command))
                    for phase, command in (
                        ("preProvision", local_cluster.spec.cluster.hooks.pre_provision),
                        ("postProvision", local_cluster.spec.cluster.hooks.post_provision),
                    )
                    if command is not None
                )
            ),
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
        run_provision_hooks: bool | None = None,
    ) -> DevelopmentClusterResult:
        """Destroy and fully converge a chart or LocalStack."""
        # All authored state is resolved before deleting a healthy cluster.
        prepared = self._prepare_target(target, profile=profile)
        hooks_enabled = (
            self.run_provision_hooks if run_provision_hooks is None else run_provision_hooks
        )
        if hooks_enabled:
            self._hooks.run("preProvision", prepared.local_cluster, cluster_name=cluster_name)
        self._destroy_environment(cluster_name)
        return self._converge_prepared(
            prepared,
            target=target,
            profile=profile,
            cluster_name=cluster_name,
            skip_installed=False,
            run_provision_hooks=hooks_enabled,
            run_pre_hook=False,
            started=time.monotonic(),
        )

    def _prepare_target(
        self, target: ResolvedLocalTarget, *, profile: str | None
    ) -> _PreparedConverge:
        """Complete every static check once, before hooks or provider mutation."""
        local_cluster = load_cluster(self._workspace)
        bootstrap_identities = bootstrap.preflight(local_cluster, root=self.root)
        steps = self._preflight_target(
            self._target_releases(target, profile=profile),
            excluded_lifecycle_identities=bootstrap_identities,
        )
        return _PreparedConverge(
            local_cluster=local_cluster,
            steps=steps,
            config=kind_config_path(self.root, local_cluster),
        )

    def _stack_release(self, source: OciChartRelease | RepoChartRelease) -> tuple[Release, str]:
        """The Helm release behind a stack's OCI or HTTPS repository entry, and its label."""
        values = tuple(self.root / path for path in source.values)
        if isinstance(source, OciChartRelease):
            release = Release(
                name=source.name,
                chart=oci_chart_ref(source),
                namespace=source.namespace,
                values=values,
                timeout=source.timeout,
                version=source.version,
            )
            return release, oci_identity(source)
        release = Release(
            name=source.name,
            chart=source.chart,
            namespace=source.namespace,
            values=values,
            timeout=source.timeout,
            version=source.version,
            repo=source.repo,
        )
        return release, source.version

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
    ) -> tuple[LifecycleRelease | OciChartRelease | RepoChartRelease, ...]:
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
        releases: tuple[LifecycleRelease | OciChartRelease | RepoChartRelease, ...],
        *,
        excluded_lifecycle_identities: frozenset[ExternallySatisfiedLifecycle] = frozenset(),
    ) -> tuple[_TargetStep, ...]:
        """Compile and validate all local identities without mutating Helm state.

        Authored order is preserved: an OCI or HTTPS repository release passes
        through as itself (there is nothing to compile), while a lifecycle
        release is replaced by the plan it resolved to.
        """
        seen: dict[Path, tuple[str, str]] = {}
        steps: list[_TargetStep] = []
        for release in releases:
            if isinstance(release, (OciChartRelease, RepoChartRelease)):
                steps.append(release)
                continue
            if not isinstance(release, LifecycleRelease):
                raise ChartManagerError(f"unsupported local release: {release!r}")
            catalog, plan = lifecycle_install_plan(self.root, release, source="local release")
            deduped: list[InstallPlanEntry] = []
            for entry in plan:
                chart = catalog.get(entry.chart)
                chart_path = chart.path.resolve()
                entry_profile = require_cluster_test_profile(chart.spec, entry.profile)
                effective_namespace = entry_profile.namespace
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
                if entry_profile.hooks is not None:
                    message = (
                        "local up does not run cluster-test hooks declared by "
                        f"{entry.chart}:{entry.profile}"
                    )
                    _LOG.warning("%s", message)
                    self._progress(warn(message))
                deduped.append(entry)
            steps.append(
                _TargetLocalExecution(
                    catalog=catalog,
                    plan=tuple(deduped),
                )
            )
        return tuple(steps)

    # ----- internals --------------------------------------------------------

    def _existing_release_keys(self, lab: Session) -> set[tuple[str, str]]:
        """Releases `--skip-installed` skips: deployed or failed, as `helm list` shows them.

        Best-effort: a failure to list falls back to "nothing installed" rather than
        aborting the converge.
        """
        try:
            releases = installed(lab)
        except ChartManagerError as exc:
            _LOG.warning(
                "helm release listing failed; treating every release as uninstalled: %s",
                exc,
            )
            self._progress(
                warn(f"could not list helm releases ({exc}); proceeding as if no releases exist")
            )
            return set()
        return {key for key, status in releases.items() if status in {"deployed", "failed"}}

    def _install_plan(
        self,
        lab: Session,
        plan: list[InstallPlanEntry],
        *,
        installed_keys: set[tuple[str, str]],
        summary: RunSummary,
        skip_installed: bool,
        cluster_tests: ClusterTestCatalog,
    ) -> None:
        """Converge each plan entry; a failed entry is recorded and the loop moves on."""
        catalog = cluster_tests
        for entry in plan:
            try:
                chart = catalog.get(entry.chart)
                profile = require_cluster_test_profile(chart.spec, entry.profile)
                values = catalog.value_paths(chart, entry.profile)
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
            self._converge_release(
                lab,
                Release(
                    name=entry.chart,
                    chart=chart.path,
                    namespace=profile.namespace,
                    values=tuple(values),
                    timeout=profile.timeout,
                ),
                entry.profile,
                installed_keys=installed_keys,
                summary=summary,
                skip_installed=skip_installed,
            )

    def _converge_release(
        self,
        lab: Session,
        release: Release,
        profile: str,
        *,
        installed_keys: set[tuple[str, str]],
        summary: RunSummary,
        skip_installed: bool,
    ) -> None:
        """Converge one release into `summary`; record a failure and keep going."""
        key = (release.namespace, release.name)
        if skip_installed and key in installed_keys:
            self._progress(
                detail("skip", f"{release.name} (already installed in {release.namespace})")
            )
            summary.no_change.append(
                DevelopmentClusterEntryOutcome(release.name, profile, release.namespace)
            )
            return
        self._progress(step("Applying", f"{release.name}:{profile} -> {release.namespace}"))
        try:
            status = converge(lab, release)
        except ChartManagerError as exc:
            if isinstance(exc, ReleaseFailed) and exc.diagnostics.strip():
                self._progress(info(exc.diagnostics))
            _LOG.error(
                "release failed; converge continues: release=%s profile=%s namespace=%s: %s",
                release.name,
                profile,
                release.namespace,
                exc,
            )
            self._progress(failure("apply failed:", f"{release.name}:{profile} -> {exc}"))
            summary.failed.append(
                DevelopmentClusterEntryFailure(
                    chart=release.name,
                    profile=profile,
                    namespace=release.namespace,
                    error=str(exc),
                )
            )
            return
        bucket = summary.applied if status == "applied" else summary.no_change
        bucket.append(DevelopmentClusterEntryOutcome(release.name, profile, release.namespace))
        installed_keys.add(key)

    # ----- bindings to the collaborator-scoped helpers -----------------------
    #
    # These four are one-line adapters: they bind `self`'s collaborators to
    # the free functions in `access.py` / `drift.py`. Keeping them as methods
    # is what lets those modules stay free of `DevelopmentClusterService` while the converge
    # engine reads the same as it did before the split.

    def _wait_apps_wildcard_ready(self, summary: RunSummary) -> None:
        """Wait for the wildcard cert, best-effort (see access.py)."""
        wait_apps_wildcard_ready(summary, kubectl=self.kubectl, progress=self._progress)

    def _access_hints(self, summary: RunSummary) -> DevelopmentClusterAccessHints:
        """Resolve the post-converge advisory data (see access.py)."""
        return access_hints(summary, kubectl=self.kubectl)

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
