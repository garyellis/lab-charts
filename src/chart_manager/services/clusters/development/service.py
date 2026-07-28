"""The converge engine: `up`, `sync`, `down`, `delete` and the install loop.

Deliberately left whole. `up`/`sync`/`_install_plan`/`_bootstrap_cilium`
share three hand-threaded mutable accumulators (`_DevelopmentClusterRunSummary`,
`installed_keys`, `namespaces_created`); cutting between them would move
that coupling across a module boundary rather than remove it. Drift
detection and access hints, which need neither the accumulators nor the
chart repository, live in `drift.py` / `access.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.clusters import bootstrap as cluster_bootstrap
from chart_manager.services.clusters.bootstrap import (
    CILIUM_BOOTSTRAP_CHART,
    CILIUM_BOOTSTRAP_NAMESPACE,
    kind_config_path,
)
from chart_manager.services.clusters.development.access import (
    access_hints,
    wait_apps_wildcard_ready,
)
from chart_manager.services.clusters.development.drift import (
    check_cilium_service_host_drift,
    warn_on_port_mapping_drift,
)
from chart_manager.services.clusters.development.models import (
    DEFAULT_CLUSTER_NAME,
    DevelopmentClusterAccessHints,
    DevelopmentClusterActionResult,
    DevelopmentClusterEntryFailure,
    DevelopmentClusterEntryOutcome,
    DevelopmentClusterResult,
    DevelopmentClusterSyncRequest,
    DevelopmentClusterUpRequest,
    _DevelopmentClusterRunSummary,
)
from chart_manager.services.domain.install_plan import DependencyResolver, InstallPlanEntry
from chart_manager.services.expose import ExposeService
from chart_manager.services.progress import (
    ProgressCallback,
    detail,
    failure,
    info,
    step,
    warn,
)
from chart_manager.settings import DEFAULT_CHARTS_DIR

# cert-manager webhook deployment. Must be Available before the
# istio-gateway chart installs (its Certificate / ClusterIssuer CRs go
# through the webhook). Subchart's default name is `cert-manager-webhook`.
CERT_MANAGER_WEBHOOK_DEPLOYMENT = "cert-manager-webhook"
CERT_MANAGER_WEBHOOK_NAMESPACE = "cert-manager"
CERT_MANAGER_WEBHOOK_TIMEOUT = "120s"
CERT_MANAGER_CHART = "cert-manager"


class DevelopmentClusterService:
    """Converge the full lab stack onto a persistent kind cluster."""

    def __init__(
        self,
        root: Path,
        *,
        helm: Helm,
        kind: Kind,
        kubectl: Kubectl,
        expose: ExposeService,
        progress: ProgressCallback | None = None,
        charts_dir: Path = DEFAULT_CHARTS_DIR,
    ) -> None:
        """Wire integrations; every cluster-facing collaborator is required.

        These used to default to `or Helm()` / `or Kubectl()`, and the CLI
        constructed the service with none of them -- so `Settings.kube_context`
        was configured in the composition root and then discarded here. The
        composition root is now the only place these are built.
        """
        self.root = root
        self.cluster_tests = ClusterTestCatalog(root, charts_dir=charts_dir)
        self.resolver = DependencyResolver(self.cluster_tests.get)
        self.helm = helm
        self.kind = kind
        self.kubectl = kubectl
        # ExposeService is injected so down/delete can stop any active
        # port-forward in the same boundary as the cluster lifecycle -- a
        # kubectl port-forward whose apiserver has just been stopped is
        # dead weight, and leaving the CLI handler to clean it up split
        # the lifecycle across two layers.
        self.expose = expose
        # No-op default so the narration call sites don't need a None check.
        self._progress: ProgressCallback = progress or (lambda _event: None)

    def up(self, options: DevelopmentClusterUpRequest) -> DevelopmentClusterResult:
        """Create/start the cluster and converge the full install plan.

        Continue-on-error: per-chart failures are recorded in the returned
        result rather than aborting the run. The result also carries the
        access hints (CA trust, URLs, grafana creds) for the surface to
        render after the summary.
        """
        self._progress(step("Ensuring sandbox cluster", options.cluster_name))
        # ensure_cluster handles absent/stopped/running uniformly: it will
        # create the cluster, start its stopped node containers, or no-op.
        self.kind.ensure_cluster(options.cluster_name, config=kind_config_path(self.root))

        # After ensure_cluster the docker containers may be up but the
        # apiserver isn't necessarily reachable yet (especially on the
        # start-stopped path). Gate before anything that talks to it --
        # `helm list -A` two lines down races otherwise.
        self._progress(step("Waiting for kube-apiserver"))
        self.kubectl.wait_apiserver_ready()

        summary = _DevelopmentClusterRunSummary()
        installed_keys: set[tuple[str, str]] = self._existing_release_keys()
        namespaces_created: set[str] = set()

        # CNI must come up before anything else. Cilium has its own
        # bootstrap branch (sets k8sServiceHost from the live control-plane
        # IP), so we own its lifecycle here regardless of whether it's in
        # the install plan.
        self._bootstrap_cilium(
            options=options,
            installed_keys=installed_keys,
            namespaces_created=namespaces_created,
            summary=summary,
        )

        plan = self.resolver.install_plan(options.chart, options.profile)
        # Filter cilium out of the plan: it's transitively pulled in by
        # grafana-dashboards:prototyping, but the bootstrap branch already
        # owns its install (and is the only place that knows the live
        # k8sServiceHost). Without this filter the summary listed cilium
        # twice -- once from bootstrap, once from the plan.
        plan = [entry for entry in plan if entry.chart != CILIUM_BOOTSTRAP_CHART]
        self._install_plan(
            plan,
            default_namespace=options.namespace,
            installed_keys=installed_keys,
            namespaces_created=namespaces_created,
            summary=summary,
            skip_installed=options.skip_installed,
        )

        # Gate URL-print on the wildcard cert being Ready: the gateway can
        # serve `https://*.<appsDomain>/` only once cert-manager has issued
        # the leaf cert. Skipping the wait would print URLs that the user's
        # browser would immediately reject with a TLS error.
        self._wait_apps_wildcard_ready(summary)

        # Warn (don't fail) on kind-config drift: editing extraPortMappings
        # without `sandbox delete && sandbox up` leaves the cluster bound to
        # the old host ports. The lab URLs we just printed would then return
        # connection-refused on the host.
        self._warn_on_port_mapping_drift(options.cluster_name)

        return summary.freeze(self._access_hints(summary, namespace=options.namespace))

    def sync(self, options: DevelopmentClusterSyncRequest) -> DevelopmentClusterResult:
        """Targeted converge: `helm upgrade --install` for the named charts only.

        Modeled on `argocd app sync <app>` and `helmfile sync -l name=<chart>`:
        same cluster-ensure + apiserver wait + cilium drift check as `up`,
        but the install loop runs only for the charts the user named. Charts
        outside the named set are skipped entirely (not even visited), so
        this is the fast way to pick up values-file edits on one or two
        charts after a large `up` has already converged the stack.

        Deliberately does NOT use `--reuse-values`: the whole point of this
        verb is to pick up values changes. If a future caller needs the
        reuse-values semantics it can be added as a flag, but doing it
        unconditionally would defeat the verb.

        Errors:
          * Unknown chart names (not in the configured install plan) raise
            `ChartManagerError` before any helm work runs, so a typo doesn't
            cause a partial converge.
        """
        if not options.chart_names:
            raise ChartManagerError("sandbox sync requires at least one chart name")

        self._progress(step("Ensuring sandbox cluster", options.cluster_name))
        self.kind.ensure_cluster(options.cluster_name, config=kind_config_path(self.root))
        self._progress(step("Waiting for kube-apiserver"))
        self.kubectl.wait_apiserver_ready()

        plan = self.resolver.install_plan(options.chart, options.profile)
        plan_charts = {entry.chart for entry in plan}
        requested = set(options.chart_names)
        # Cilium isn't a member of the plan we just resolved (it's bootstrap-
        # owned and filtered out below), but it IS a legal sync target: a
        # dev who edited cilium values needs a way to reconverge it.
        valid_targets = plan_charts | {CILIUM_BOOTSTRAP_CHART}
        unknown = sorted(requested - valid_targets)
        if unknown:
            raise ChartManagerError(
                f"chart(s) {unknown} not in the install plan for {options.chart}:{options.profile}"
            )

        installed_keys = self._existing_release_keys()
        namespaces_created: set[str] = set()
        summary = _DevelopmentClusterRunSummary()

        # Drift check still runs (it's cheap and the dev should hear about
        # a broken cilium before we try to upgrade an unrelated chart on
        # top of an apiserver-unreachable network).
        cilium_key = (CILIUM_BOOTSTRAP_NAMESPACE, CILIUM_BOOTSTRAP_CHART)
        if cilium_key in installed_keys:
            self._check_cilium_service_host_drift(options.cluster_name)

        # If the user explicitly asked to sync cilium, run the bootstrap
        # branch (it's the only path that knows the live control-plane IP).
        if CILIUM_BOOTSTRAP_CHART in requested:
            self._bootstrap_cilium(
                options=DevelopmentClusterUpRequest(
                    chart=options.chart,
                    profile=options.profile,
                    cluster_name=options.cluster_name,
                    namespace=options.namespace,
                ),
                installed_keys=installed_keys,
                namespaces_created=namespaces_created,
                summary=summary,
                force=True,
            )

        # Build a sub-plan filtered to the requested charts (and not cilium,
        # which we just handled). Preserve original plan ordering so a sync
        # of multiple charts still respects their declared dependency order.
        sub_plan = [
            entry
            for entry in plan
            if entry.chart in requested and entry.chart != CILIUM_BOOTSTRAP_CHART
        ]
        self._install_plan(
            sub_plan,
            default_namespace=options.namespace,
            installed_keys=installed_keys,
            namespaces_created=namespaces_created,
            summary=summary,
            skip_installed=False,
        )

        self._wait_apps_wildcard_ready(summary)

        return summary.freeze(self._access_hints(summary, namespace=options.namespace))

    def _bootstrap_cilium(
        self,
        *,
        options: DevelopmentClusterUpRequest,
        installed_keys: set[tuple[str, str]],
        namespaces_created: set[str],
        summary: _DevelopmentClusterRunSummary,
        force: bool = False,
    ) -> None:
        """Install / converge / drift-check cilium.

        Three branches:
          1. Installed AND `skip_installed=True` AND not `force` -> drift
             check, then record as no-change and return. Fast-skip path.
          2. Installed -> drift check, then converge (helm decides no-op
             vs upgrade). Default path.
          3. Not installed -> run the bootstrap (sets k8sServiceHost from
             the live control-plane IP, which only this branch can do).

        `force=True` collapses (1) into (2) so `sync cilium` always
        converges regardless of `skip_installed`.
        """
        cilium_key = (CILIUM_BOOTSTRAP_NAMESPACE, CILIUM_BOOTSTRAP_CHART)
        cilium_installed = cilium_key in installed_keys

        if cilium_installed:
            # Drift gate runs in BOTH the skip and converge paths: re-running
            # `helm upgrade cilium` against a stale k8sServiceHost would
            # itself silently break the cluster, so we want the loud error
            # before any helm work touches CNI.
            self._check_cilium_service_host_drift(options.cluster_name)

        if cilium_installed and options.skip_installed and not force:
            self._progress(
                detail(
                    "skip",
                    f"cilium (already installed in {CILIUM_BOOTSTRAP_NAMESPACE})",
                )
            )
            summary.no_change.append(
                DevelopmentClusterEntryOutcome(
                    CILIUM_BOOTSTRAP_CHART,
                    "minimal",
                    CILIUM_BOOTSTRAP_NAMESPACE,
                )
            )
            namespaces_created.add(CILIUM_BOOTSTRAP_NAMESPACE)
            return

        try:
            result = cluster_bootstrap.bootstrap(
                options.cluster_name,
                helm=self.helm,
                kind=self.kind,
                kubectl=self.kubectl,
                cluster_tests=self.cluster_tests,
                progress=self._progress,
                lint=False,
            )
        except (ExternalCommandError, ChartManagerError) as exc:
            # Continue-on-error: cilium failure leaves the rest of the
            # plan to surface its own errors and lets the dev decide.
            self._progress(failure("cilium bootstrap failed:", str(exc)))
            summary.failed.append(
                DevelopmentClusterEntryFailure(
                    chart=CILIUM_BOOTSTRAP_CHART,
                    profile="minimal",
                    namespace=CILIUM_BOOTSTRAP_NAMESPACE,
                    error=str(exc),
                )
            )
            return

        # bootstrap() returns the helm status, or None when the cilium
        # chart is absent and bootstrap was skipped entirely.
        if result is None:
            return
        bucket = summary.applied if result == "applied" else summary.no_change
        bucket.append(
            DevelopmentClusterEntryOutcome(
                CILIUM_BOOTSTRAP_CHART,
                "minimal",
                CILIUM_BOOTSTRAP_NAMESPACE,
            )
        )
        installed_keys.add(cilium_key)
        namespaces_created.add(CILIUM_BOOTSTRAP_NAMESPACE)

    def down(self, cluster_name: str = DEFAULT_CLUSTER_NAME) -> DevelopmentClusterActionResult:
        """Stop the cluster's node containers; preserve all state.

        State preserved by `docker stop`: etcd, installed Helm releases,
        PVCs, and the containerd image cache inside the node containers. A
        subsequent `up` re-uses the same containers (no image re-pull) and
        converges every chart through `helm upgrade --install` (which
        helm itself no-ops when nothing changed); pass `--skip-installed`
        to `up` for the prior fast-skip behavior.

        Also stops any active `sandbox expose` port-forward for this
        cluster -- a kubectl port-forward whose apiserver has just been
        stopped will exit on its own, but we reap it explicitly so the
        state file is cleared and the next `sandbox expose` can start
        without an "already running" error.
        """
        self._progress(step("Stopping sandbox cluster", cluster_name))
        stopped = self.kind.stop_cluster(cluster_name)
        return DevelopmentClusterActionResult(
            cluster_name=cluster_name,
            changed=stopped,
            port_forward_pid=self.expose.stop(cluster_name),
        )

    def delete(self, cluster_name: str = DEFAULT_CLUSTER_NAME) -> DevelopmentClusterActionResult:
        """Tear down the cluster entirely (`kind delete cluster`).

        Destructive: image cache, etcd, and any data in node-local PVs are
        gone. Use `down` if you want a fast restart. Any active port-forward
        is stopped for the same reason as `down`.
        """
        self._progress(step("Deleting sandbox cluster", cluster_name))
        deleted = self.kind.delete_cluster(cluster_name)
        return DevelopmentClusterActionResult(
            cluster_name=cluster_name,
            changed=deleted,
            port_forward_pid=self.expose.stop(cluster_name),
        )

    # ----- internals --------------------------------------------------------

    def _existing_release_keys(self) -> set[tuple[str, str]]:
        """Snapshot of (namespace, release-name) pairs already installed.

        Used to skip charts on re-run. Best-effort: a failure to list (no
        kubeconfig, cluster just created and apiserver still settling, etc.)
        falls back to "nothing installed" rather than aborting.
        """
        try:
            releases = self.helm.list_releases(all_namespaces=True)
        except ExternalCommandError as exc:
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
        summary: _DevelopmentClusterRunSummary,
        skip_installed: bool,
    ) -> None:
        """Converge each plan entry, bucketing outcomes into `summary`.

        Continue-on-error: a failed entry is recorded and the loop moves
        on. Mutates `installed_keys` / `namespaces_created` in place.
        """
        for entry in plan:
            try:
                chart = self.cluster_tests.get(entry.chart)
            except ChartManagerError as exc:
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

            # Profile lookup is inside the guard too. `spec.profile()` raises
            # SpecError for an unknown name, and sitting outside every try it
            # aborted the entire plan -- so one chart whose `requires:` named
            # a renamed profile took down an 18-chart converge instead of
            # being recorded as a single failed row, contradicting the
            # continue-on-error contract this method documents.
            try:
                profile = chart.spec.profile(entry.profile)
            except ChartManagerError as exc:
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

            if namespace not in namespaces_created:
                self.kubectl.create_namespace(namespace)
                namespaces_created.add(namespace)

            try:
                values = self.cluster_tests.value_paths(chart, entry.profile)
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
                    self._post_install_hook(entry.chart, namespace)
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
            except (ExternalCommandError, ChartManagerError) as exc:
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

    def _post_install_hook(self, chart: str, namespace: str) -> None:
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
            except (ExternalCommandError, ChartManagerError) as exc:
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

    def _wait_apps_wildcard_ready(self, summary: _DevelopmentClusterRunSummary) -> None:
        """Wait for the wildcard cert, best-effort (see access.py)."""
        wait_apps_wildcard_ready(summary, kubectl=self.kubectl, progress=self._progress)

    def _access_hints(
        self, summary: _DevelopmentClusterRunSummary, *, namespace: str
    ) -> DevelopmentClusterAccessHints:
        """Resolve the post-converge advisory data (see access.py)."""
        return access_hints(summary, kubectl=self.kubectl, namespace=namespace)

    def _warn_on_port_mapping_drift(self, cluster_name: str) -> None:
        """Warn on kind-config host-port drift (see drift.py)."""
        warn_on_port_mapping_drift(
            cluster_name, kind=self.kind, root=self.root, progress=self._progress
        )

    def _check_cilium_service_host_drift(self, cluster_name: str) -> None:
        """Hard-fail on a confirmed cilium k8sServiceHost mismatch (see drift.py)."""
        check_cilium_service_host_drift(
            cluster_name, kind=self.kind, helm=self.helm, progress=self._progress
        )
