"""Top-level Typer app wiring and small inline commands for chart-manager."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from chart_manager.cli import events as events_cli
from chart_manager.cli import helmrelease as helmrelease_cli
from chart_manager.cli import lifecycle as lifecycle_cli
from chart_manager.cli import upgrade as upgrade_cli
from chart_manager.cli import validate as validate_cli
from chart_manager.composition import Container
from chart_manager.plumbing.errors import ChartManagerError, MissingToolError
from chart_manager.services.chart_catalog import ChartCatalogService
from chart_manager.services.clusters.development import (
    DEFAULT_CHART as DEVELOPMENT_DEFAULT_CHART,
)
from chart_manager.services.clusters.development import (
    DEFAULT_PROFILE as DEVELOPMENT_DEFAULT_PROFILE,
)
from chart_manager.services.clusters.development import (
    LAB_CA_SECRET_NAME,
    LAB_CA_SECRET_NAMESPACE,
    DevelopmentClusterAccessHints,
    DevelopmentClusterActionResult,
    DevelopmentClusterResult,
    DevelopmentClusterSyncRequest,
    DevelopmentClusterUpRequest,
)
from chart_manager.services.clusters.ephemeral import (
    DEFAULT_CLUSTER_NAME,
    DEFAULT_NAMESPACE,
    DEFAULT_PROFILE,
    EphemeralTestRequest,
)
from chart_manager.services.expose import ExposeRequest
from chart_manager.services.install_plan import InstallPlanService
from chart_manager.services.progress import ProgressEvent

console = Console()


def _container() -> Container:
    """Build the composition root for one CLI invocation.

    Module-level, mirroring `cli/helmrelease.py` and `cli/validate.py`, so a
    test can monkeypatch the whole wiring in one place. Every cluster-facing
    service below is built through it: constructing them inline is what let
    `Settings.kube_context` be configured and then ignored.
    """
    return Container()


# Severity -> Rich style for the narration the long-running services emit.
# The service picks the severity; only this table knows it becomes markup.
_PROGRESS_STYLES: dict[str, str | None] = {
    "step": "bold",
    "detail": "dim",
    "warn": "yellow",
    "error": "red",
    "info": None,
}


def _print_progress(event: ProgressEvent) -> None:
    """Render one service progress event to the console.

    The event's `label` carries the severity emphasis and `message` stays
    plain, which reproduces the `[bold]Applying[/bold] chart:profile` shape
    the services used to build themselves. A label-less event emphasizes
    the whole line.

    Both fields are escaped before they reach Rich. They carry subprocess
    output -- helm/kubectl stderr, raw `kubectl get events` dumps -- and an
    unmatched closing tag (a bracketed path like `[/etc/hosts]`, a JSON
    Patch path, an XML fragment) raises MarkupError. That turned the one
    diagnostic an operator needs into a traceback.
    """
    style = _PROGRESS_STYLES.get(event.severity)
    message = escape(event.message)
    label = None if event.label is None else escape(event.label)
    if label is None:
        text = f"[{style}]{message}[/{style}]" if style else message
    elif style:
        text = f"[{style}]{label}[/{style}] {message}".rstrip()
    else:
        text = f"{label} {message}".rstrip()
    console.print(text)


def _exit_if_failed(ok: bool) -> None:
    """The surface's single exit-code rule: a not-ok result is exit 1.

    Services report partial failure on the result object rather than by
    raising, so a surface that only renders it reports success for a run in
    which charts failed.
    """
    if not ok:
        raise typer.Exit(1)


app = typer.Typer(no_args_is_help=True, help="Local and CI workflows for lab Helm charts.")
charts_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect Helm charts and their lifecycle intent.",
)
deps_app = typer.Typer(no_args_is_help=True, help="Resolve test dependencies.")
sandbox_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Local development cluster lifecycle. "
        "Bring up the full stack, exercise individual charts, expose services, "
        "stop, or delete."
    ),
)
ci_app = typer.Typer(no_args_is_help=True, help="CI-oriented helpers.")
helmrelease_app = typer.Typer(
    no_args_is_help=True,
    help="Operate on Flux HelmRelease resources in a separate GitOps repo.",
)
# Grafana-specific subcommands. Anything that knows about Grafana JSON / API
# conventions lives here, not under the generic `charts` group.
grafana_app = typer.Typer(no_args_is_help=True, help="Grafana-specific tooling.")
validate_app = typer.Typer(
    no_args_is_help=True,
    help="Static chart validation: render -> schema -> policy.",
)
lifecycle_app = typer.Typer(
    no_args_is_help=True,
    help="Compile lifecycle intent into plans, graphs, diagnostics, and status.",
)

# setup the events command interface
events_app = typer.Typer(no_args_is_help=True, help="Emit platform lifecycle events.")

events_cli.register(events_app)
validate_cli.register(validate_app)
helmrelease_cli.register(helmrelease_app)
lifecycle_cli.register(lifecycle_app)
upgrade_cli.register(app)

app.add_typer(events_app, name="events")
app.add_typer(charts_app, name="charts")
app.add_typer(deps_app, name="deps")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(ci_app, name="ci")
app.add_typer(grafana_app, name="grafana")
app.add_typer(validate_app, name="validate")
app.add_typer(helmrelease_app, name="helmrelease")
app.add_typer(lifecycle_app, name="lifecycle")

RootOption = Annotated[Path, typer.Option("--root", help="Repository root.")]
ProfileOption = Annotated[str, typer.Option("--profile", help="Cluster-test profile.")]
ClusterNameOption = Annotated[str, typer.Option("--cluster-name", help="kind cluster name.")]
NamespaceOption = Annotated[str, typer.Option("--namespace", help="Kubernetes namespace.")]


@charts_app.command("list")
def list_charts(root: RootOption = Path(".")) -> None:
    """List Helm charts and their lifecycle capability status."""
    service = ChartCatalogService(root)
    table = Table(
        "Chart",
        "Type",
        "Version",
        "Dependencies",
        "Lifecycle",
        "Manifest validation",
        "Cluster tests",
        "Profiles",
    )
    invalid = False
    for entry in service.list_entries():
        invalid = invalid or entry.error is not None
        lifecycle_status = (
            f"[red]invalid: {escape(entry.error or '')}[/red]"
            if entry.error is not None
            else entry.lifecycle_status
        )
        table.add_row(
            entry.name,
            entry.chart_type,
            entry.version,
            ", ".join(entry.dependencies),
            lifecycle_status,
            entry.validation.value,
            entry.cluster_test.value,
            ", ".join(entry.profiles),
        )
    console.print(table)
    if invalid:
        raise typer.Exit(1)


@charts_app.command("lifecycle")
def show_lifecycle(chart: str, root: RootOption = Path(".")) -> None:
    """Print one chart's normalized ChartLifecycle intent."""
    lifecycle = ChartCatalogService(root).get_lifecycle(chart)
    console.print_json(
        data=lifecycle.model_dump(mode="json", by_alias=True, exclude_none=True)
    )


@grafana_app.command("export-dashboard")
def grafana_export_dashboard(
    uid: Annotated[str, typer.Argument(help="Dashboard UID to export.")],
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    namespace: NamespaceOption = DEFAULT_NAMESPACE,
    release: Annotated[
        str,
        typer.Option(
            "--release", help="Grafana Helm release name (drives secret and service name)."
        ),
    ] = "grafana",
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Write the normalized JSON to this file (default: stdout)."
        ),
    ] = None,
) -> None:
    """Export a dashboard from a kind-deployed Grafana and normalize for git.

    Auth + connectivity are resolved from the cluster: the admin password is
    read from secret/<release>, then an ephemeral port-forward to svc/<release>
    carries the HTTP GET. No pre-existing port-forward required.
    """
    from chart_manager.services.grafana.dashboard_export import ExportRequest

    payload = (
        _container()
        .grafana_exporter()
        .export(
            ExportRequest(
                uid=uid,
                cluster_name=cluster_name,
                namespace=namespace,
                release=release,
            )
        )
    )
    if output is None:
        sys.stdout.write(payload)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload)
        console.print(f"[green]wrote[/green] {output}")


@grafana_app.command("lint-dashboards")
def grafana_lint_dashboards(
    root: RootOption = Path("."),
    path: Annotated[
        list[Path],
        typer.Option(
            "--path",
            help="Specific dashboard JSON file (repeatable). Default: all under charts/grafana-dashboards/dashboards/.",
        ),
    ] = [],
) -> None:
    """Lint Grafana dashboards for repo-wide quality rules."""
    from chart_manager.services.grafana.dashboard_lint import discover_dashboards, lint_paths

    targets = list(path) if path else discover_dashboards(root)
    if not targets:
        console.print("[yellow]no dashboards found[/yellow]")
        raise typer.Exit(0)

    result = lint_paths(targets)
    for finding in result.findings:
        console.print(finding.render())

    if not result.ok:
        console.print(
            f"\n[red]{len(result.findings)} findings across "
            f"{result.files_with_findings}/{result.files_scanned} dashboards[/red]"
        )
        raise typer.Exit(1)
    console.print(f"[green]ok[/green]: {result.files_scanned} dashboards passed")


@deps_app.command("plan")
def dependency_plan(
    chart: str,
    root: RootOption = Path("."),
    profile: ProfileOption = DEFAULT_PROFILE,
) -> None:
    service = InstallPlanService(root)
    table = Table("Order", "Chart", "Profile", "Target")
    for index, entry in enumerate(service.install_plan(chart, profile), start=1):
        table.add_row(str(index), entry.chart, entry.profile, "yes" if entry.target else "")
    console.print(table)


@deps_app.command("checks")
def dependency_checks(
    chart: str,
    root: RootOption = Path("."),
    profile: ProfileOption = DEFAULT_PROFILE,
) -> None:
    service = InstallPlanService(root)
    table = Table("Order", "Chart", "Profile", "Check", "Type", "Description")
    row = 0
    for entry in service.plan_checks(chart, profile):
        for check in entry.checks:
            row += 1
            table.add_row(
                str(row),
                entry.chart,
                entry.profile,
                check.name,
                check.type,
                check.description or "",
            )
    console.print(table)


@deps_app.command("dependent-tests")
def dependent_tests(chart: str, root: RootOption = Path(".")) -> None:
    service = InstallPlanService(root)
    table = Table("Chart", "Profile")
    for ref in service.dependent_tests(chart):
        table.add_row(ref.chart, ref.profile)
    console.print(table)


@sandbox_app.command("ensure")
def ensure_kind(
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    root: RootOption = Path("."),
) -> None:
    # No progress callback: this command's whole output is the one line
    # below, so the service's "Ensuring sandbox cluster" step would be noise.
    _container().ephemeral_test_cluster_service(root.resolve()).ensure_cluster(cluster_name)
    console.print(f"sandbox cluster ready: {cluster_name}")


@sandbox_app.command("up")
def sandbox_up(
    chart: Annotated[
        str,
        typer.Option(
            "--chart",
            help="Entry chart whose profile is the install plan source.",
        ),
    ] = DEVELOPMENT_DEFAULT_CHART,
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            help="Profile on --chart to resolve into the install plan.",
        ),
    ] = DEVELOPMENT_DEFAULT_PROFILE,
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    namespace: NamespaceOption = DEFAULT_NAMESPACE,
    skip_installed: Annotated[
        bool,
        typer.Option(
            "--skip-installed",
            help=(
                "Skip charts already present in `helm list -A`. Faster, "
                "but won't pick up values changes."
            ),
        ),
    ] = False,
    root: RootOption = Path("."),
) -> None:
    """Bring up the sandbox cluster and install the full stack.

    Works whether the cluster is missing, stopped, or already running:
    `kind ensure_cluster` handles all three. Continue-on-error: a failing
    chart is reported in the summary but does not abort the run.

    Default: converge -- every chart in the install plan runs `helm
    upgrade --install`, helm itself no-ops the ones whose rendered
    manifests haven't changed. This is the helmfile/Argo workflow and
    picks up values-file edits on re-run. Pass `--skip-installed` to
    restore the prior fast-skip behavior (don't even invoke helm for
    releases already in `helm list -A`).
    """
    service = _container().development_cluster_service(root, progress=_print_progress)
    result = service.up(
        DevelopmentClusterUpRequest(
            chart=chart,
            profile=profile,
            cluster_name=cluster_name,
            namespace=namespace,
            skip_installed=skip_installed,
        )
    )
    _render_development_cluster_result(result)
    _exit_if_failed(result.ok)


@sandbox_app.command("sync")
def sandbox_sync(
    chart_names: Annotated[
        list[str],
        typer.Argument(
            min=1,
            help="Chart names to re-apply (must be members of the install plan).",
        ),
    ],
    chart: Annotated[
        str,
        typer.Option(
            "--chart",
            help="Entry chart whose profile is the install plan source.",
        ),
    ] = DEVELOPMENT_DEFAULT_CHART,
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            help="Profile on --chart to resolve into the install plan.",
        ),
    ] = DEVELOPMENT_DEFAULT_PROFILE,
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    namespace: NamespaceOption = DEFAULT_NAMESPACE,
    root: RootOption = Path("."),
) -> None:
    """Re-apply specific charts (pick up values edits without a full up).

    Runs `helm upgrade --install` for ONLY the named charts. Charts not
    named are not visited. Useful after editing a values file on one chart
    when the rest of the stack is already converged.

    Errors if any named chart is not a member of the configured install
    plan, so a typo can't quietly do nothing.
    """
    service = _container().development_cluster_service(root, progress=_print_progress)
    result = service.sync(
        DevelopmentClusterSyncRequest(
            chart_names=tuple(chart_names),
            chart=chart,
            profile=profile,
            cluster_name=cluster_name,
            namespace=namespace,
        )
    )
    _render_development_cluster_result(result)
    _exit_if_failed(result.ok)


@sandbox_app.command("down")
def sandbox_down(
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    root: RootOption = Path("."),
) -> None:
    """Stop the sandbox cluster's containers; preserve all state.

    `docker stop` on the kind node containers. Installed Helm releases,
    PVCs, etcd, and the containerd image cache survive. Use `sandbox up`
    to bring it back. Any active port-forward for this cluster is also
    stopped, since its kubectl process will lose the apiserver anyway.
    """
    _render_cluster_action(
        _container().development_cluster_service(root, progress=_print_progress).down(cluster_name),
        verb="stopped",
        absent="not running",
    )


@sandbox_app.command("delete")
def sandbox_delete(
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    root: RootOption = Path("."),
) -> None:
    """Tear down the sandbox cluster entirely.

    `kind delete cluster`: destructive, the image cache goes with it and
    the next `sandbox up` will re-pull. Use `sandbox down` if you just
    want to stop the cluster.
    """
    _render_cluster_action(
        _container()
        .development_cluster_service(root, progress=_print_progress)
        .delete(cluster_name),
        verb="deleted",
        absent="not present",
    )


def _render_development_cluster_result(result: DevelopmentClusterResult) -> None:
    """Print the converge summary table, then the access hints.

    Order is operational and load-bearing: what happened, then how to reach
    it. Everything here is pure formatting -- every decision (which bucket,
    whether the CA hint applies, which URLs exist) was already made by
    DevelopmentClusterService and arrives on the result.
    """
    table = Table("Status", "Chart", "Profile", "Namespace", title="Lab install summary")
    for entry in result.applied:
        table.add_row("[green]applied[/green]", entry.chart, entry.profile, entry.namespace)
    for entry in result.no_change:
        table.add_row("[dim]no-change[/dim]", entry.chart, entry.profile, entry.namespace)
    for failed in result.failed:
        table.add_row("[red]failed[/red]", failed.chart, failed.profile, failed.namespace)
    console.print(table)
    if not result.ok:
        console.print(f"[red]{len(result.failed)} chart(s) failed[/red]; see diagnostics above")
    _render_access_hints(result.hints)


def _render_access_hints(hints: DevelopmentClusterAccessHints) -> None:
    """Print the CA-trust block and the URL block for a finished converge."""
    if hints.ca_trust_hint:
        _print_ca_import_hint()
    if hints.urls_error is not None:
        console.print(f"[yellow]warn:[/yellow] {hints.urls_error}")
        return
    if not hints.urls:
        return
    console.print("\n[bold]URLs:[/bold]")
    for url in hints.urls:
        console.print(f"  {url}")
        if url != hints.grafana_url:
            continue
        if hints.grafana_error is not None:
            console.print(
                f"    [yellow]could not read admin password:[/yellow] {hints.grafana_error}"
            )
        elif hints.grafana_credentials is not None:
            user, password = hints.grafana_credentials
            console.print(f"    user: {user}\n    pass: {password}")


def _print_ca_import_hint() -> None:
    """Print the one-time CA-trust instructions for the lab self-signed CA.

    The wildcard *.localhost cert is signed by an in-cluster CA
    (charts/istio-gateway/templates/cert-manager-ca.yaml). Until the CA is
    trusted, browsers show a cert warning on every <app>.localhost page.
    This is a one-time keychain operation; the hint is printed every run
    because we cannot cheaply tell whether the user has already imported it.

    The `security add-trusted-cert` one-liner is gated on Darwin -- emitting
    it on Linux would be misleading (the tool doesn't exist there). Linux
    devs get the generic "import into your OS trust store" line, which is
    enough -- most Linux desktops differ on whether the store lives in NSS,
    p11-kit, or update-ca-certificates.
    """
    cmd = (
        f"kubectl get secret {LAB_CA_SECRET_NAME} "
        f"-n {LAB_CA_SECRET_NAMESPACE} "
        "-o jsonpath='{.data.tls\\.crt}' | base64 -d > ~/lab-ca.crt"
    )
    console.print("\n[bold]Trust the lab CA[/bold] (one-time, per workstation):")
    console.print(f"  [dim]{cmd}[/dim]")
    console.print("  Then import ~/lab-ca.crt into your OS keychain and mark it trusted.")
    if sys.platform == "darwin":
        macos_trust = (
            "security add-trusted-cert -d -r trustRoot "
            "-k ~/Library/Keychains/login.keychain-db ~/lab-ca.crt"
        )
        console.print(f"  [dim]macOS one-liner: {macos_trust}[/dim]")
    console.print(
        "  [dim]Re-import after every 'sandbox delete' -- the lab CA is "
        "regenerated each fresh install.[/dim]"
    )
    console.print(
        "  [dim]Firefox users: also set network.dns.localDomains = "
        '"localhost" in about:config[/dim]'
    )
    console.print(
        "  [dim]Optional for curl/k6: "
        'echo "127.0.0.1 grafana.localhost" | sudo tee -a /etc/hosts[/dim]'
    )


def _render_cluster_action(
    result: DevelopmentClusterActionResult, *, verb: str, absent: str
) -> None:
    """Print the outcome of `down` / `delete` plus any port-forward we reaped."""
    state = verb if result.changed else absent
    console.print(f"sandbox cluster {state}: {result.cluster_name}")
    if result.port_forward_pid is not None:
        console.print(f"stopped port-forward (pid {result.port_forward_pid})")


@sandbox_app.command("expose")
def kind_expose(
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    service: Annotated[
        str,
        typer.Option("--service", help="namespace/name of the Service to forward."),
    ] = "istio-ingress/istio-gateway",
    port: Annotated[
        list[str],
        typer.Option(
            "--port", "-p", help="LOCAL:REMOTE mapping (repeatable). Defaults to 8443:443 8080:80."
        ),
    ] = [],
    stop: Annotated[
        bool, typer.Option("--stop", help="Stop the running port-forward for this cluster.")
    ] = False,
) -> None:
    expose = _container().expose_service()

    if stop:
        stopped = expose.stop(cluster_name)
        if stopped is None:
            console.print(f"no port-forward state for cluster [bold]{cluster_name}[/bold]")
        else:
            console.print(f"stopped port-forward (pid {stopped})")
        return

    # An empty --port list is the "use the lab defaults" signal; ExposeRequest
    # owns which mappings that means.
    status = expose.start(
        ExposeRequest(cluster_name=cluster_name, service=service, ports=list(port))
    )

    console.print(
        f"[bold]port-forward running[/bold] (pid {status.pid})  "
        f"cluster={cluster_name}  service={service}"
    )
    for exposed in status.urls:
        console.print(f"  {exposed.url}  ->  {service}:{exposed.remote_port}")
    console.print(f"  log:  {status.log}")
    console.print(f"  stop: chart-manager sandbox expose --cluster-name {cluster_name} --stop")


@sandbox_app.command("test")
def sandbox_test(
    chart: str,
    root: RootOption = Path("."),
    profile: ProfileOption = DEFAULT_PROFILE,
    namespace: NamespaceOption = DEFAULT_NAMESPACE,
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    dependent_tests: Annotated[
        bool,
        typer.Option(
            "--dependent-tests",
            help="Run cluster tests affected by this chart.",
        ),
    ] = False,
    no_ensure_cluster: Annotated[
        bool,
        typer.Option("--no-ensure-cluster", help="Do not create the sandbox cluster if missing."),
    ] = False,
    lint: Annotated[bool, typer.Option("--lint", help="Run helm lint before install.")] = False,
) -> None:
    service = _container().ephemeral_test_cluster_service(root, progress=_print_progress)
    service.run(
        EphemeralTestRequest(
            chart=chart,
            profile=profile,
            namespace=namespace,
            cluster_name=cluster_name,
            ensure_cluster=not no_ensure_cluster,
            include_dependent_tests=dependent_tests,
            lint=lint,
        )
    )


@ci_app.command("changed")
def ci_changed(
    root: RootOption = Path("."),
    base: Annotated[str, typer.Option("--base", help="Git comparison base.")] = "origin/main",
) -> None:
    service = _container().ci_service(root)
    for chart in service.changed_charts(base):
        console.print(chart)


@ci_app.command("cluster-test-charts")
def ci_cluster_test_charts(root: RootOption = Path(".")) -> None:
    """List every chart enabled for live-cluster tests."""
    for chart in _container().ci_service(root).cluster_test_charts():
        console.print(chart)


@ci_app.command("cluster-test-matrix")
def ci_cluster_test_matrix(
    root: RootOption = Path("."),
    base: Annotated[str, typer.Option("--base", help="Git comparison base.")] = "origin/main",
    all_charts: Annotated[
        bool,
        typer.Option("--all", help="Include every chart with enabled cluster tests."),
    ] = False,
    charts: Annotated[
        list[str] | None,
        typer.Option(
            "--chart",
            help="Explicit chart to include; repeat for multiple charts.",
        ),
    ] = None,
) -> None:
    """Emit a GitHub-ready chart/profile cluster-test matrix as JSON."""
    if all_charts and charts:
        raise ChartManagerError("--all and --chart are mutually exclusive")
    service = _container().ci_service(root)
    if all_charts:
        entries = service.all_cluster_test_matrix()
    elif charts:
        entries = service.explicit_cluster_test_matrix(charts)
    else:
        entries = service.cluster_test_matrix(base)
    payload = {
        "include": [
            {"chart": entry.chart, "profile": entry.profile}
            for entry in entries
        ]
    }
    typer.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))


@ci_app.command("install")
def ci_install(
    chart: str,
    root: RootOption = Path("."),
    profile: ProfileOption = DEFAULT_PROFILE,
    namespace: NamespaceOption = DEFAULT_NAMESPACE,
) -> None:
    _container().ci_service(root).install_source_chart(chart, profile, namespace)


@ci_app.command("upgrade")
def ci_upgrade(
    chart: str,
    oci_ref: Annotated[
        str, typer.Option("--from-oci", help="OCI chart ref for the main-branch artifact.")
    ],
    root: RootOption = Path("."),
    profile: ProfileOption = DEFAULT_PROFILE,
    namespace: NamespaceOption = DEFAULT_NAMESPACE,
) -> None:
    _container().ci_service(root).upgrade_from_oci(chart, profile, namespace, oci_ref)


def main() -> None:
    """Entry point: map domain errors to exit 1 and an absent tool to 127.

    MissingToolError is caught first because it subclasses ChartManagerError.
    127 is the shell's "command not found", so it is reserved for a genuinely
    absent binary -- a wrapper keying on it to say "install helm" must not
    fire because a *data* file was missing, which is what catching bare
    FileNotFoundError here used to do.
    """
    try:
        app()
    except MissingToolError as exc:
        console.print(f"[red]error:[/red] {escape(str(exc))}")
        sys.exit(127)
    except ChartManagerError as exc:
        console.print(f"[red]error:[/red] {escape(str(exc))}")
        sys.exit(1)
    except FileNotFoundError as exc:
        console.print(f"[red]error:[/red] file not found: {escape(str(exc.filename or exc))}")
        sys.exit(1)
