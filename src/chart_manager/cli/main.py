from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from chart_manager.cli import events as events_cli
from chart_manager.cli import helmrelease as helmrelease_cli
from chart_manager.cli import validate as validate_cli
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.spec import CheckSpec
from chart_manager.services.charts import ChartService
from chart_manager.services.ci import CiService
from chart_manager.services.dependencies import DependencyService
from chart_manager.services.expose import ExposeRequest, ExposeService
from chart_manager.services.lab import (
    DEFAULT_CHART as LAB_DEFAULT_CHART,
)
from chart_manager.services.lab import (
    DEFAULT_PROFILE as LAB_DEFAULT_PROFILE,
)
from chart_manager.services.lab import (
    LabService,
    LabSyncOptions,
    LabUpOptions,
)
from chart_manager.services.sandbox import (
    DEFAULT_CLUSTER_NAME,
    DEFAULT_NAMESPACE,
    DEFAULT_PROFILE,
    SandboxOptions,
    SandboxService,
)

console = Console()

app = typer.Typer(no_args_is_help=True, help="Local and CI workflows for lab Helm charts.")
charts_app = typer.Typer(no_args_is_help=True, help="Inspect charts and test specs.")
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

# setup the events command interface
events_app = typer.Typer(no_args_is_help=True, help="Emit platform lifecycle events.")

events_cli.register(events_app)
validate_cli.register(validate_app)
helmrelease_cli.register(helmrelease_app)

app.add_typer(events_app, name="events")
app.add_typer(charts_app, name="charts")
app.add_typer(deps_app, name="deps")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(ci_app, name="ci")
app.add_typer(grafana_app, name="grafana")
app.add_typer(validate_app, name="validate")
app.add_typer(helmrelease_app, name="helmrelease")

RootOption = Annotated[Path, typer.Option("--root", help="Repository root.")]
ProfileOption = Annotated[str, typer.Option("--profile", help="test-spec profile.")]
ClusterNameOption = Annotated[str, typer.Option("--cluster-name", help="kind cluster name.")]
NamespaceOption = Annotated[str, typer.Option("--namespace", help="Kubernetes namespace.")]


@charts_app.command("list")
def list_charts(root: RootOption = Path(".")) -> None:
    service = ChartService(root)
    table = Table("Chart", "Version", "Dependencies", "Profiles")
    for name in service.list_charts():
        try:
            chart = service.get_chart(name)
        except ChartManagerError:
            table.add_row(name, "?", "?", "[red]<no test-spec>[/red]")
            continue
        version = chart.chart_yaml.get("version", "") or ""
        deps_raw = chart.chart_yaml.get("dependencies") or []
        deps: list[dict[str, object]] = deps_raw if isinstance(deps_raw, list) else []
        dep_versions = ", ".join(
            f"{dep.get('name', '?')} {dep.get('version', '?')}" for dep in deps
        )
        profiles = ", ".join(chart.spec.profiles)
        table.add_row(name, str(version), dep_versions, profiles)
    console.print(table)


@charts_app.command("spec")
def show_spec(chart: str, root: RootOption = Path(".")) -> None:
    service = ChartService(root)
    model = service.get_chart(chart)
    console.print_json(data=model.spec.model_dump(by_alias=True))


@grafana_app.command("export-dashboard")
def grafana_export_dashboard(
    uid: Annotated[str, typer.Argument(help="Dashboard UID to export.")],
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    namespace: NamespaceOption = DEFAULT_NAMESPACE,
    release: Annotated[
        str,
        typer.Option("--release", help="Grafana Helm release name (drives secret and service name)."),
    ] = "grafana",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the normalized JSON to this file (default: stdout)."),
    ] = None,
) -> None:
    """Export a dashboard from a kind-deployed Grafana and normalize for git.

    Auth + connectivity are resolved from the cluster: the admin password is
    read from secret/<release>, then an ephemeral port-forward to svc/<release>
    carries the HTTP GET. No pre-existing port-forward required.
    """
    from chart_manager.services.grafana.dashboard_export import (
        ExportRequest,
        GrafanaExporter,
    )

    dashboard = GrafanaExporter().fetch(
        ExportRequest(
            uid=uid,
            cluster_name=cluster_name,
            namespace=namespace,
            release=release,
        )
    )
    payload = json.dumps(dashboard, sort_keys=True, indent=2) + "\n"
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

    findings = lint_paths(targets)
    for finding in findings:
        console.print(finding.render())

    n_files = len(targets)
    n_bad = len({f.path for f in findings})
    if findings:
        console.print(f"\n[red]{len(findings)} findings across {n_bad}/{n_files} dashboards[/red]")
        raise typer.Exit(1)
    console.print(f"[green]ok[/green]: {n_files} dashboards passed")


@deps_app.command("plan")
def dependency_plan(
    chart: str,
    root: RootOption = Path("."),
    profile: ProfileOption = DEFAULT_PROFILE,
) -> None:
    service = DependencyService(root)
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
    service = DependencyService(root)
    repository = service.repository
    table = Table("Order", "Chart", "Profile", "Check", "Type", "Description")
    row = 0
    for entry in service.install_plan(chart, profile):
        chart_model = repository.get(entry.chart)
        profile_model = chart_model.spec.profile(entry.profile)
        checks = profile_model.checks or []
        if profile_model.helm_test and not any(check.type == "helm-test" for check in checks):
            checks = [
                *checks,
                CheckSpec(
                    name="helm-test",
                    type="helm-test",
                    description="Run Helm test hooks for the release.",
                ),
            ]
        for check in checks:
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


@deps_app.command("reverse")
def reverse_tests(chart: str, root: RootOption = Path(".")) -> None:
    service = DependencyService(root)
    table = Table("Chart", "Profile")
    for ref in service.reverse_tests(chart):
        table.add_row(ref.chart, ref.profile)
    console.print(table)


@sandbox_app.command("ensure")
def ensure_kind(
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    root: RootOption = Path("."),
) -> None:
    service = SandboxService(root)
    kind_config = root.resolve() / "kind-config.yaml"
    service.kind.ensure_cluster(
        cluster_name,
        config=kind_config if kind_config.exists() else None,
    )
    console.print(f"sandbox cluster ready: {cluster_name}")


@sandbox_app.command("up")
def sandbox_up(
    chart: Annotated[
        str,
        typer.Option(
            "--chart",
            help="Entry chart whose profile is the install plan source.",
        ),
    ] = LAB_DEFAULT_CHART,
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            help="Profile on --chart to resolve into the install plan.",
        ),
    ] = LAB_DEFAULT_PROFILE,
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
    service = LabService(root)
    service.up(
        LabUpOptions(
            chart=chart,
            profile=profile,
            cluster_name=cluster_name,
            namespace=namespace,
            skip_installed=skip_installed,
        )
    )


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
    ] = LAB_DEFAULT_CHART,
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            help="Profile on --chart to resolve into the install plan.",
        ),
    ] = LAB_DEFAULT_PROFILE,
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
    service = LabService(root)
    service.sync(
        LabSyncOptions(
            chart_names=tuple(chart_names),
            chart=chart,
            profile=profile,
            cluster_name=cluster_name,
            namespace=namespace,
        )
    )


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
    LabService(root).down(cluster_name)


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
    LabService(root).delete(cluster_name)


_APPS_DOMAIN_FALLBACK = "localhost"


def _detect_apps_domain(kubectl: Kubectl) -> str:
    """Best-effort apps-domain detection from installed Gateways.

    Reads `kubectl get gateway -A`, harvests `.spec.servers[].hosts[]`,
    strips the wildcard `*.` prefix, and returns the most-common host
    suffix. Ties break on alphabetical order so output is reproducible.
    Falls back to `localhost` when no Gateway is installed yet (pre-lab,
    or a single-chart sandbox test) -- consistent with the appsDomain
    default in charts/istio-gateway/values-ci.yaml.

    Pure function over `kubectl.list_gateway_hosts()` so tests can mock
    a single call and assert the derived domain.
    """
    try:
        hosts = kubectl.list_gateway_hosts()
    except ChartManagerError:
        return _APPS_DOMAIN_FALLBACK
    suffixes: list[str] = []
    for host in hosts:
        stripped = host[2:] if host.startswith("*.") else host
        if stripped:
            suffixes.append(stripped)
    if not suffixes:
        return _APPS_DOMAIN_FALLBACK
    counts = Counter(suffixes)
    # Counter.most_common is insertion-stable on ties; pick the
    # alphabetically smallest suffix in the top-frequency band so the
    # output is deterministic regardless of host iteration order.
    top_freq = max(counts.values())
    return min(s for s, c in counts.items() if c == top_freq)


@sandbox_app.command("expose")
def kind_expose(
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    service: Annotated[
        str,
        typer.Option("--service", help="namespace/name of the Service to forward."),
    ] = "istio-ingress/istio-gateway",
    port: Annotated[
        list[str],
        typer.Option("--port", "-p", help="LOCAL:REMOTE mapping (repeatable). Defaults to 8443:443 8080:80."),
    ] = [],
    stop: Annotated[bool, typer.Option("--stop", help="Stop the running port-forward for this cluster.")] = False,
) -> None:
    expose = ExposeService()

    if stop:
        stopped = expose.stop(cluster_name)
        if stopped is None:
            console.print(f"no port-forward state for cluster [bold]{cluster_name}[/bold]")
        else:
            console.print(f"stopped port-forward (pid {stopped})")
        return

    ports = list(port) if port else ["8443:443", "8080:80"]
    status = expose.start(ExposeRequest(cluster_name=cluster_name, service=service, ports=ports))

    # Apps-domain is per-CLI-invocation: cache the kubectl result so the
    # per-mapping print loop doesn't re-list Gateways on every iteration.
    apps_domain = _detect_apps_domain(Kubectl())

    console.print(
        f"[bold]port-forward running[/bold] (pid {status.pid})  "
        f"cluster={cluster_name}  service={service}"
    )
    for mapping in ports:
        local, remote = mapping.split(":", 1)
        scheme = "https" if remote in {"443", "8443"} else "http"
        console.print(f"  {scheme}://*.{apps_domain}:{local}/  ->  {service}:{remote}")
    console.print(f"  log:  {status.log}")
    console.print(f"  stop: chart-manager sandbox expose --cluster-name {cluster_name} --stop")


@sandbox_app.command("test")
def sandbox_test(
    chart: str,
    root: RootOption = Path("."),
    profile: ProfileOption = DEFAULT_PROFILE,
    namespace: NamespaceOption = DEFAULT_NAMESPACE,
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    reverse: Annotated[bool, typer.Option("--reverse", help="Run reverse dependency tests.")] = False,
    no_ensure_cluster: Annotated[
        bool,
        typer.Option("--no-ensure-cluster", help="Do not create the sandbox cluster if missing."),
    ] = False,
    lint: Annotated[bool, typer.Option("--lint", help="Run helm lint before install.")] = False,
) -> None:
    service = SandboxService(root)
    service.run(
        SandboxOptions(
            chart=chart,
            profile=profile,
            namespace=namespace,
            cluster_name=cluster_name,
            ensure_cluster=not no_ensure_cluster,
            include_reverse=reverse,
            lint=lint,
        )
    )


@ci_app.command("changed")
def ci_changed(
    root: RootOption = Path("."),
    base: Annotated[str, typer.Option("--base", help="Git comparison base.")] = "origin/main",
) -> None:
    service = CiService(root)
    for chart in service.changed_charts(base):
        console.print(chart)


@ci_app.command("install")
def ci_install(
    chart: str,
    root: RootOption = Path("."),
    profile: ProfileOption = DEFAULT_PROFILE,
    namespace: NamespaceOption = DEFAULT_NAMESPACE,
) -> None:
    CiService(root).install_source_chart(chart, profile, namespace)


@ci_app.command("upgrade")
def ci_upgrade(
    chart: str,
    oci_ref: Annotated[str, typer.Option("--from-oci", help="OCI chart ref for the main-branch artifact.")],
    root: RootOption = Path("."),
    profile: ProfileOption = DEFAULT_PROFILE,
    namespace: NamespaceOption = DEFAULT_NAMESPACE,
) -> None:
    CiService(root).upgrade_from_oci(chart, profile, namespace, oci_ref)


def main() -> None:
    try:
        app()
    except ChartManagerError as exc:
        console.print(f"[red]error:[/red] {exc}")
        sys.exit(1)
    except FileNotFoundError as exc:
        console.print(f"[red]error:[/red] required binary not found: {exc.filename or exc}")
        sys.exit(127)
