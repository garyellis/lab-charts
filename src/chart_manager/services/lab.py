"""LabService -- long-lived dev cluster lifecycle for the full stack.

Contrast with `SandboxService`:
  - SandboxService = ephemeral, one chart's smoke test, fail-fast, runs
    `helm test` per chart. CI-shaped.
  - LabService     = persistent, the whole observability stack from the
    grafana-dashboards `prototyping` profile, continue-on-error so a single
    flaky chart doesn't block iteration on the rest, NO `helm test` calls.
    Developer-shaped.

Lifecycle verbs (surfaced as `chart-manager sandbox up|down|delete`):

  - up     : create or start the cluster, then install the stack.
             `Kind.ensure_cluster` handles the absent/stopped/running cases,
             so `up` works whether the cluster has never been created, was
             stopped via `sandbox down`, or is already running.
  - down   : `docker stop` the cluster's node containers. Preserves etcd,
             installed Helm releases, PVCs, and the containerd image cache.
             Fast restart via `up`. No image re-pull.
  - delete : `kind delete cluster` -- full teardown, image cache and
             release state are gone, next `up` re-pulls everything.

The persistent cluster is intentionally named `chart-manager` (the same name
SandboxService uses): one human developer is not running both at once, and
sharing the name lets `sandbox test` and `sandbox up/down/delete` cooperate
on the same kind cluster.

Readiness contract is rollout-status only (Kubectl.wait_workloads_ready),
the same gate SandboxService uses between install and `helm test`.
"""
from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import Any, Final

import yaml
from rich.console import Console
from rich.table import Table

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.charts import ChartRepository
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.plumbing.graph import DependencyResolver, PlanEntry
from chart_manager.services import cluster_bootstrap
from chart_manager.services.cluster_bootstrap import (
    CILIUM_BOOTSTRAP_CHART,
    CILIUM_BOOTSTRAP_NAMESPACE,
    KIND_CONFIG_FILENAME,
)
from chart_manager.services.expose import ExposeService

DEFAULT_CLUSTER_NAME = "chart-manager"
DEFAULT_CHART = "grafana-dashboards"
DEFAULT_PROFILE = "prototyping"
DEFAULT_NAMESPACE = "observability"

GRAFANA_RELEASE = "grafana"
GRAFANA_ADMIN_SECRET_KEY = "admin-password"

# Lab CA Certificate (and the namespace it lives in) issued by the
# istio-gateway chart's cert-manager-ca.yaml. The wildcard `*.<appsDomain>`
# leaf cert that the gateway listener serves; we gate URL-print on it being
# Ready so the first browser hit isn't a TLS error.
APPS_WILDCARD_CERT_NAME = "apps-wildcard"
APPS_WILDCARD_CERT_NAMESPACE = "istio-ingress"
APPS_WILDCARD_CERT_TIMEOUT = "120s"

# cert-manager webhook deployment. Must be Available before the
# istio-gateway chart installs (its Certificate / ClusterIssuer CRs go
# through the webhook). Subchart's default name is `cert-manager-webhook`.
CERT_MANAGER_WEBHOOK_DEPLOYMENT = "cert-manager-webhook"
CERT_MANAGER_WEBHOOK_NAMESPACE = "cert-manager"
CERT_MANAGER_WEBHOOK_TIMEOUT = "120s"
CERT_MANAGER_CHART = "cert-manager"

# In-cluster CA secret produced by the lab cert-manager bootstrap. The
# one-line keychain-import hint printed at the end of `up` references this
# exact name+namespace (defined in charts/istio-gateway/templates/
# cert-manager-ca.yaml).
LAB_CA_SECRET_NAME = "lab-root-ca-secret"
LAB_CA_SECRET_NAMESPACE = "cert-manager"

# Charts whose successful install means the lab CA cert chain is in place
# and worth telling the user to trust. istio-gateway is the chart that
# owns the cert-manager ClusterIssuers + the root CA Certificate; if it
# applied or no-changed cleanly, the secret exists.
LAB_CA_OWNER_CHART = "istio-gateway"

# `helm get values` for the cilium release surfaces the `k8sServiceHost`
# we passed at install time under the cilium subchart key. Detecting drift
# means walking this exact path -- a missing key is benign (older install,
# or chart restructure) and falls through to the warning branch.
CILIUM_SERVICE_HOST_PATH: Final[tuple[str, ...]] = ("cilium", "k8sServiceHost")


@dataclass(frozen=True)
class LabUpOptions:
    chart: str = DEFAULT_CHART
    profile: str = DEFAULT_PROFILE
    cluster_name: str = DEFAULT_CLUSTER_NAME
    namespace: str = DEFAULT_NAMESPACE
    # Converge-by-default is the helmfile/Argo workflow: every chart in the
    # install plan runs `helm upgrade --install`, helm itself no-ops the
    # ones that haven't changed. `skip_installed=True` restores the prior
    # behavior of skipping anything already in `helm list -A` -- faster on
    # large stacks but silently ignores values-file edits, which is exactly
    # the surprise that motivated the converge-by-default flip.
    skip_installed: bool = False


@dataclass(frozen=True)
class LabSyncOptions:
    """Args for `LabService.sync` -- targeted upgrade of named charts.

    Reuses `LabUpOptions` fields (chart/profile drive the install-plan
    membership check, cluster_name + namespace drive cluster ensure /
    default namespace) but is a distinct type because `skip_installed`
    has no meaning for sync (it's already a targeted-converge verb).
    """

    chart_names: tuple[str, ...]
    chart: str = DEFAULT_CHART
    profile: str = DEFAULT_PROFILE
    cluster_name: str = DEFAULT_CLUSTER_NAME
    namespace: str = DEFAULT_NAMESPACE


@dataclass
class _EntryFailure:
    chart: str
    profile: str
    namespace: str
    error: str


@dataclass
class _RunSummary:
    """Per-run accounting of converge outcomes for the summary table.

    Buckets mirror helmfile/Argo terminology:
      * applied:   helm produced a new release revision
      * no_change: helm returned 0 and revision was unchanged (deep no-op)
      * failed:    subprocess error; release may or may not be in a good state
    """

    # Load-bearing tuple shape: (chart, profile, namespace) -- callers index
    # by position (entry[0] for chart membership tests in `_grafana_reachable`
    # / `_lab_ca_present`). A dataclass would be cleaner; deferred until a
    # second positional use lands.
    applied: list[tuple[str, str, str]] = field(default_factory=list)
    no_change: list[tuple[str, str, str]] = field(default_factory=list)
    failed: list[_EntryFailure] = field(default_factory=list)


class LabService:
    def __init__(
        self,
        root: Path,
        *,
        helm: Helm | None = None,
        kind: Kind | None = None,
        kubectl: Kubectl | None = None,
        expose: ExposeService | None = None,
        console: Console | None = None,
    ) -> None:
        self.root = root
        self.repository = ChartRepository(root)
        self.resolver = DependencyResolver(self.repository)
        self.helm = helm or Helm()
        self.kind = kind or Kind()
        self.kubectl = kubectl or Kubectl()
        # ExposeService is injected so down/delete can stop any active
        # port-forward in the same boundary as the cluster lifecycle -- a
        # kubectl port-forward whose apiserver has just been stopped is
        # dead weight, and leaving the CLI handler to clean it up split
        # the lifecycle across two layers.
        self.expose = expose or ExposeService()
        self.console = console or Console()

    def up(self, options: LabUpOptions) -> None:
        self.console.print(f"[bold]Ensuring sandbox cluster[/bold] {options.cluster_name}")
        kind_config = self.root / KIND_CONFIG_FILENAME
        # ensure_cluster handles absent/stopped/running uniformly: it will
        # create the cluster, start its stopped node containers, or no-op.
        self.kind.ensure_cluster(
            options.cluster_name,
            config=kind_config if kind_config.exists() else None,
        )

        # After ensure_cluster the docker containers may be up but the
        # apiserver isn't necessarily reachable yet (especially on the
        # start-stopped path). Gate before anything that talks to it --
        # `helm list -A` two lines down races otherwise.
        self.console.print("[bold]Waiting for kube-apiserver[/bold]")
        self.kubectl.wait_apiserver_ready()

        summary = _RunSummary()
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

        self._print_summary(summary)
        self._print_access_hints(summary, namespace=options.namespace)

    def sync(self, options: LabSyncOptions) -> None:
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

        self.console.print(f"[bold]Ensuring sandbox cluster[/bold] {options.cluster_name}")
        kind_config = self.root / KIND_CONFIG_FILENAME
        self.kind.ensure_cluster(
            options.cluster_name,
            config=kind_config if kind_config.exists() else None,
        )
        self.console.print("[bold]Waiting for kube-apiserver[/bold]")
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
                f"chart(s) {unknown} not in the install plan for "
                f"{options.chart}:{options.profile}"
            )

        installed_keys = self._existing_release_keys()
        namespaces_created: set[str] = set()
        summary = _RunSummary()

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
                options=LabUpOptions(
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

        self._print_summary(summary)
        self._print_access_hints(summary, namespace=options.namespace)

    def _bootstrap_cilium(
        self,
        *,
        options: LabUpOptions,
        installed_keys: set[tuple[str, str]],
        namespaces_created: set[str],
        summary: _RunSummary,
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
            self.console.print(
                f"[dim]skip[/dim] cilium (already installed in "
                f"{CILIUM_BOOTSTRAP_NAMESPACE})"
            )
            summary.no_change.append(
                (CILIUM_BOOTSTRAP_CHART, "minimal", CILIUM_BOOTSTRAP_NAMESPACE)
            )
            namespaces_created.add(CILIUM_BOOTSTRAP_NAMESPACE)
            return

        try:
            result = cluster_bootstrap.bootstrap(
                options.cluster_name,
                helm=self.helm,
                kind=self.kind,
                kubectl=self.kubectl,
                repository=self.repository,
                console=self.console,
                lint=False,
            )
        except (ExternalCommandError, ChartManagerError) as exc:
            # Continue-on-error: cilium failure leaves the rest of the
            # plan to surface its own errors and lets the dev decide.
            self.console.print(f"[red]cilium bootstrap failed:[/red] {exc}")
            summary.failed.append(
                _EntryFailure(
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
        bucket.append((CILIUM_BOOTSTRAP_CHART, "minimal", CILIUM_BOOTSTRAP_NAMESPACE))
        installed_keys.add(cilium_key)
        namespaces_created.add(CILIUM_BOOTSTRAP_NAMESPACE)

    def down(self, cluster_name: str = DEFAULT_CLUSTER_NAME) -> None:
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
        self.console.print(f"[bold]Stopping sandbox cluster[/bold] {cluster_name}")
        stopped = self.kind.stop_cluster(cluster_name)
        if stopped:
            self.console.print(f"sandbox cluster stopped: {cluster_name}")
        else:
            self.console.print(
                f"sandbox cluster not running: {cluster_name}"
            )
        self._stop_port_forward(cluster_name)

    def delete(self, cluster_name: str = DEFAULT_CLUSTER_NAME) -> None:
        """Tear down the cluster entirely (`kind delete cluster`).

        Destructive: image cache, etcd, and any data in node-local PVs are
        gone. Use `down` if you want a fast restart. Any active port-forward
        is stopped for the same reason as `down`.
        """
        self.console.print(f"[bold]Deleting sandbox cluster[/bold] {cluster_name}")
        deleted = self.kind.delete_cluster(cluster_name)
        if deleted:
            self.console.print(f"sandbox cluster deleted: {cluster_name}")
        else:
            self.console.print(f"sandbox cluster not present: {cluster_name}")
        self._stop_port_forward(cluster_name)

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
            self.console.print(
                f"[yellow]warn:[/yellow] could not list helm releases ({exc}); "
                "proceeding as if no releases exist"
            )
            return set()
        return {(r.namespace, r.name) for r in releases}

    def _install_plan(
        self,
        plan: list[PlanEntry],
        *,
        default_namespace: str,
        installed_keys: set[tuple[str, str]],
        namespaces_created: set[str],
        summary: _RunSummary,
        skip_installed: bool,
    ) -> None:
        for entry in plan:
            try:
                chart = self.repository.get(entry.chart)
            except ChartManagerError as exc:
                self.console.print(
                    f"[red]chart resolution failed:[/red] {entry.chart}: {exc}"
                )
                summary.failed.append(
                    _EntryFailure(
                        chart=entry.chart,
                        profile=entry.profile,
                        namespace="?",
                        error=str(exc),
                    )
                )
                continue

            profile = chart.spec.profile(entry.profile)
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
                self.console.print(
                    f"[dim]skip[/dim] {entry.chart}:{entry.profile} "
                    f"(already installed in {namespace})"
                )
                summary.no_change.append((entry.chart, entry.profile, namespace))
                namespaces_created.add(namespace)
                continue

            if namespace not in namespaces_created:
                self.kubectl.create_namespace(namespace)
                namespaces_created.add(namespace)

            try:
                values = self.repository.value_paths(chart, entry.profile)
                self.console.print(f"[bold]Updating dependencies[/bold] {entry.chart}")
                # mtime-gated: skips the subprocess when Chart.lock is
                # already newer than Chart.yaml and charts/ is populated.
                # Per-chart per-process cache prevents repeat fetches.
                self.helm.dependency_update_if_stale(chart.path)
                self.console.print(
                    f"[bold]Applying[/bold] {entry.chart}:{entry.profile} -> {namespace}"
                )
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
                    self.console.print(
                        f"[bold]Waiting for workloads[/bold] {entry.chart}"
                    )
                    self.kubectl.wait_workloads_ready(namespace, timeout=profile.timeout)
                    self._post_install_hook(entry.chart, namespace)
                else:
                    # No-change: nothing is rolling, so the rollout-status
                    # wait would just be a no-op against the existing
                    # generation. Skipping it is the biggest single time
                    # savings on a converge-with-no-edits re-run. Print a
                    # dim marker so the skip is observable in the run log.
                    self.console.print(
                        f"[dim]no change[/dim] {entry.chart} (rollout wait skipped)"
                    )
                bucket = summary.applied if applied else summary.no_change
                bucket.append((entry.chart, entry.profile, namespace))
                installed_keys.add(key)
            except (ExternalCommandError, ChartManagerError) as exc:
                self.console.print(
                    f"[red]apply failed:[/red] {entry.chart}:{entry.profile} -> {exc}"
                )
                summary.failed.append(
                    _EntryFailure(
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
            self.console.print(
                f"[bold]Waiting for[/bold] Deployment/{CERT_MANAGER_WEBHOOK_DEPLOYMENT} "
                f"-n {CERT_MANAGER_WEBHOOK_NAMESPACE}"
            )
            try:
                self.kubectl.wait_deployment_available(
                    CERT_MANAGER_WEBHOOK_DEPLOYMENT,
                    namespace=CERT_MANAGER_WEBHOOK_NAMESPACE,
                    timeout=CERT_MANAGER_WEBHOOK_TIMEOUT,
                )
            except (ExternalCommandError, ChartManagerError) as exc:
                self.console.print(
                    f"[yellow]warn:[/yellow] cert-manager webhook not Available "
                    f"({exc}); subsequent CR submissions may fail"
                )

    @contextmanager
    def _diagnostics_on_failure(self, namespace: str) -> Iterator[None]:
        # Mirror SandboxService's pattern: dump pods+events on subprocess
        # failure, then re-raise so the install loop's try/except records
        # the failure and moves on.
        try:
            yield
        except ExternalCommandError:
            diagnostics = self.kubectl.diagnostics(namespace)
            if diagnostics.strip():
                self.console.print(diagnostics)
            raise

    def _print_summary(self, summary: _RunSummary) -> None:
        table = Table("Status", "Chart", "Profile", "Namespace", title="Lab install summary")
        for chart, profile, namespace in summary.applied:
            table.add_row("[green]applied[/green]", chart, profile, namespace)
        for chart, profile, namespace in summary.no_change:
            table.add_row("[dim]no-change[/dim]", chart, profile, namespace)
        for failure in summary.failed:
            table.add_row(
                "[red]failed[/red]", failure.chart, failure.profile, failure.namespace
            )
        self.console.print(table)
        if summary.failed:
            self.console.print(
                f"[red]{len(summary.failed)} chart(s) failed[/red]; see diagnostics above"
            )

    def _grafana_reachable(self, summary: _RunSummary) -> bool:
        # Treat "applied this run" or "no-change because already converged"
        # as good enough to advertise the URL. A failure entry suppresses it.
        return any(
            entry[0] == GRAFANA_RELEASE
            for entry in chain(summary.applied, summary.no_change)
        )

    def _lab_ca_present(self, summary: _RunSummary) -> bool:
        # The istio-gateway chart owns the cert-manager ClusterIssuer chain
        # (lab -> lab-root-ca -> lab-ca-issuer) and the wildcard cert. If it
        # synced cleanly, the lab CA secret should exist; either bucket
        # (applied or no-change) is sufficient -- no-change means it already
        # existed from a prior run.
        return any(
            entry[0] == LAB_CA_OWNER_CHART
            for entry in chain(summary.applied, summary.no_change)
        )

    def _wait_apps_wildcard_ready(self, summary: _RunSummary) -> None:
        """Block until `Certificate/apps-wildcard` reports Ready=True.

        Only runs if the istio-gateway chart was part of this run (applied
        or no-change). In any other path (e.g. `sync grafana`) the wildcard
        cert is either pre-existing-and-Ready or simply outside the scope
        of this run. Best-effort: a `kubectl wait` failure is surfaced as
        a warning rather than aborting, because the URL print is itself
        an advisory.
        """
        if not self._lab_ca_present(summary):
            return
        self.console.print(
            f"[bold]Waiting for[/bold] Certificate/{APPS_WILDCARD_CERT_NAME} "
            f"-n {APPS_WILDCARD_CERT_NAMESPACE}"
        )
        try:
            self.kubectl.wait_certificate_ready(
                APPS_WILDCARD_CERT_NAME,
                namespace=APPS_WILDCARD_CERT_NAMESPACE,
                timeout=APPS_WILDCARD_CERT_TIMEOUT,
            )
        except (ExternalCommandError, ChartManagerError) as exc:
            self.console.print(
                f"[yellow]warn:[/yellow] apps-wildcard cert not Ready "
                f"({exc}); URLs below may serve a TLS error until cert-manager catches up"
            )

    def _warn_on_port_mapping_drift(self, cluster_name: str) -> None:
        """Diff the kind-config host ports against the live container.

        Kind bakes `extraPortMappings` into the node container spec at
        cluster-create time. Editing kind-config.yaml then `sandbox down`
        + `sandbox up` does NOT re-apply the mapping (docker start preserves
        the old container spec). Detect that and print a warning row so the
        dev knows a `sandbox delete && sandbox up` is required.
        """
        expected = self._kind_config_host_ports()
        if not expected:
            return
        try:
            live = self.kind.container_host_ports(cluster_name)
        except (ExternalCommandError, ChartManagerError) as exc:
            self.console.print(
                f"[yellow]warn:[/yellow] could not inspect container "
                f"port mappings ({exc}); skipping drift check"
            )
            return
        missing = expected - live
        if not missing:
            return
        self.console.print(
            f"[yellow]warn:[/yellow] kind cluster port mappings do not "
            f"match kind-config.yaml (missing host ports: "
            f"{sorted(missing)}); run "
            "'sandbox delete && sandbox up' to apply."
        )

    def _kind_config_host_ports(self) -> set[int]:
        """Parse `extraPortMappings[].hostPort` from the repo's kind-config.

        Returns the empty set if the file is missing / malformed -- in that
        case there's nothing to compare against, so the drift check is a
        no-op. Limited to control-plane node mapping which is the only one
        kind-config.yaml currently declares.
        """
        kind_config = self.root / KIND_CONFIG_FILENAME
        if not kind_config.is_file():
            return set()
        try:
            data = yaml.safe_load(kind_config.read_text()) or {}
        except yaml.YAMLError:
            return set()
        ports: set[int] = set()
        for node in data.get("nodes") or []:
            for mapping in (node or {}).get("extraPortMappings") or []:
                host_port = (mapping or {}).get("hostPort")
                if isinstance(host_port, int):
                    ports.add(host_port)
        return ports

    def _check_cilium_service_host_drift(self, cluster_name: str) -> None:
        """Fail loud if cilium's pinned k8sServiceHost no longer matches.

        Best-effort: an unreadable values payload (release just removed,
        kubeconfig drift, etc.) warns and continues -- we don't want a
        diagnostic helper to block the install plan. But a *confirmed*
        mismatch is a hard fail, because cilium with the wrong apiserver
        VIP silently breaks all kube-proxy-replacement traffic.
        """
        try:
            current_ip = self.kind.control_plane_ip(cluster_name)
        except (ExternalCommandError, ChartManagerError) as exc:
            self.console.print(
                f"[yellow]warn:[/yellow] could not read control-plane IP "
                f"for drift check ({exc}); skipping"
            )
            return

        try:
            values = self.helm.get_values(
                CILIUM_BOOTSTRAP_CHART, namespace=CILIUM_BOOTSTRAP_NAMESPACE
            )
        except ExternalCommandError as exc:
            self.console.print(
                f"[yellow]warn:[/yellow] could not read cilium release values "
                f"for drift check ({exc}); skipping"
            )
            return

        installed_ip = _walk(values, CILIUM_SERVICE_HOST_PATH)

        if installed_ip is None:
            self.console.print(
                "[yellow]warn:[/yellow] cilium release has no pinned "
                "k8sServiceHost; skipping drift check"
            )
            return

        if str(installed_ip) != current_ip:
            raise ChartManagerError(
                f"cilium k8sServiceHost drift: installed={installed_ip} "
                f"current={current_ip}; run 'mise run sandbox-delete' then "
                f"'mise run sandbox-up' to recover"
            )

    def _stop_port_forward(self, cluster_name: str) -> None:
        stopped = self.expose.stop(cluster_name)
        if stopped is not None:
            self.console.print(f"stopped port-forward (pid {stopped})")

    def _print_access_hints(self, summary: _RunSummary, *, namespace: str) -> None:
        """Single entry point for the post-install advisory block.

        Order is operational: trust the CA (no warnings on first GET) ->
        URLs (one per VirtualService host). Both halves are best-effort
        and silent when no relevant chart was synced this run.
        """
        if self._lab_ca_present(summary):
            self._print_ca_import_hint()
        self._print_virtualservice_urls(summary, namespace=namespace)

    def _print_virtualservice_urls(
        self, summary: _RunSummary, *, namespace: str
    ) -> None:
        """List one URL per VirtualService host; tack grafana creds underneath.

        Source of truth is `kubectl get virtualservice -A`: as more apps
        get wired through the gateway, their VS templates surface here
        automatically without code changes. Stable ordering (sorted by
        host) so a no-change re-run produces byte-identical output.

        Best-effort: missing CRD / empty list prints nothing in this block.
        """
        try:
            hosts = self.kubectl.list_virtualservice_hosts()
        except (ExternalCommandError, ChartManagerError) as exc:
            self.console.print(
                f"[yellow]warn:[/yellow] could not list VirtualServices "
                f"({exc}); skipping URL hints"
            )
            return
        if not hosts:
            return
        # Defensive sort even though `list_virtualservice_hosts` already
        # returns sorted output -- keeps the contract local to the print
        # site so a future kubectl helper change can't quietly destabilize
        # the rendered URL block.
        self.console.print("\n[bold]URLs:[/bold]")
        for host in sorted(hosts):
            self.console.print(f"  https://{host}/")
            if host.startswith(f"{GRAFANA_RELEASE}."):
                self._print_grafana_credentials(namespace=namespace)

    def _print_grafana_credentials(self, *, namespace: str) -> None:
        try:
            password = self.kubectl.get_secret_value(
                GRAFANA_RELEASE,
                GRAFANA_ADMIN_SECRET_KEY,
                namespace=namespace,
            )
        except ChartManagerError as exc:
            self.console.print(
                f"    [yellow]could not read admin password:[/yellow] {exc}"
            )
            return
        self.console.print(f"    user: admin\n    pass: {password}")

    def _print_ca_import_hint(self) -> None:
        """Print a one-time CA-trust hint for the lab self-signed CA.

        The wildcard *.localhost cert is signed by an in-cluster CA
        (charts/istio-gateway/templates/cert-manager-ca.yaml). Until the CA
        is trusted, browsers show a cert warning on every <app>.localhost
        page. This is a one-time keychain operation; the hint is printed
        every run because we cannot cheaply tell whether the user has
        already imported it.

        The `security add-trusted-cert` one-liner is gated on Darwin --
        emitting it on Linux would be misleading (the tool doesn't exist
        there). Linux devs get the generic "import into your OS trust
        store" line, which is enough -- most Linux desktops differ on
        whether the store lives in NSS, p11-kit, or update-ca-certificates.
        """
        cmd = (
            f"kubectl get secret {LAB_CA_SECRET_NAME} "
            f"-n {LAB_CA_SECRET_NAMESPACE} "
            "-o jsonpath='{.data.tls\\.crt}' | base64 -d > ~/lab-ca.crt"
        )
        self.console.print(
            "\n[bold]Trust the lab CA[/bold] (one-time, per workstation):"
        )
        self.console.print(f"  [dim]{cmd}[/dim]")
        self.console.print(
            "  Then import ~/lab-ca.crt into your OS keychain and mark it trusted."
        )
        if sys.platform == "darwin":
            macos_trust = (
                "security add-trusted-cert -d -r trustRoot "
                "-k ~/Library/Keychains/login.keychain-db ~/lab-ca.crt"
            )
            self.console.print(f"  [dim]macOS one-liner: {macos_trust}[/dim]")
        self.console.print(
            "  [dim]Re-import after every 'sandbox delete' -- the lab CA is "
            "regenerated each fresh install.[/dim]"
        )
        self.console.print(
            "  [dim]Firefox users: also set network.dns.localDomains = "
            '"localhost" in about:config[/dim]'
        )
        self.console.print(
            "  [dim]Optional for curl/k6: "
            'echo "127.0.0.1 grafana.localhost" | sudo tee -a /etc/hosts[/dim]'
        )


def _walk(data: Mapping[str, Any], path: tuple[str, ...]) -> object | None:
    """Descend into a nested mapping by `path`; return None on any miss.

    "Miss" = a path segment is absent, or an intermediate node is not a
    Mapping. Used for drift detection where the values payload may
    legitimately be older / restructured and we want a single "not
    present" signal rather than a sequence of KeyError / TypeError
    branches. Accepts any Mapping so callers handing us a yaml-loaded
    dict-like aren't forced to copy.
    """
    cursor: object = data
    for key in path:
        if not isinstance(cursor, Mapping) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor
