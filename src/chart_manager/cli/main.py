from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.spec import CheckSpec
from chart_manager.services.charts import ChartService
from chart_manager.services.ci import CiService
from chart_manager.services.dependencies import DependencyService
from chart_manager.services.expose import ExposeRequest, ExposeService
from chart_manager.services.kind_test import (
    DEFAULT_CLUSTER_NAME,
    DEFAULT_NAMESPACE,
    DEFAULT_PROFILE,
    KindTestOptions,
    KindTestService,
)

console = Console()

app = typer.Typer(no_args_is_help=True, help="Local and CI workflows for lab Helm charts.")
charts_app = typer.Typer(no_args_is_help=True, help="Inspect charts and test specs.")
deps_app = typer.Typer(no_args_is_help=True, help="Resolve test dependencies.")
kind_app = typer.Typer(no_args_is_help=True, help="Run kind-backed chart tests.")
ci_app = typer.Typer(no_args_is_help=True, help="CI-oriented helpers.")
# Grafana-specific subcommands. Anything that knows about Grafana JSON / API
# conventions lives here, not under the generic `charts` group.
grafana_app = typer.Typer(no_args_is_help=True, help="Grafana-specific tooling.")

app.add_typer(charts_app, name="charts")
app.add_typer(deps_app, name="deps")
app.add_typer(kind_app, name="kind")
app.add_typer(ci_app, name="ci")
app.add_typer(grafana_app, name="grafana")

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
        deps = chart.chart_yaml.get("dependencies") or []
        dep_versions = ", ".join(
            f"{dep.get('name', '?')} {dep.get('version', '?')}" for dep in deps
        )
        profiles = ", ".join(chart.spec.profiles)
        table.add_row(name, version, dep_versions, profiles)
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


@kind_app.command("ensure")
def ensure_kind(
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    root: RootOption = Path("."),
) -> None:
    service = KindTestService(root)
    kind_config = root.resolve() / "kind-config.yaml"
    service.kind.ensure_cluster(
        cluster_name,
        config=kind_config if kind_config.exists() else None,
    )
    console.print(f"kind cluster ready: {cluster_name}")


@kind_app.command("expose")
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

    console.print(
        f"[bold]port-forward running[/bold] (pid {status.pid})  "
        f"cluster={cluster_name}  service={service}"
    )
    for mapping in ports:
        local, remote = mapping.split(":", 1)
        scheme = "https" if remote in {"443", "8443"} else "http"
        console.print(f"  {scheme}://*.kind.local:{local}/  ->  {service}:{remote}")
    console.print(f"  log:  {status.log}")
    console.print(f"  stop: chart-manager kind expose --cluster-name {cluster_name} --stop")


@kind_app.command("test")
def kind_test(
    chart: str,
    root: RootOption = Path("."),
    profile: ProfileOption = DEFAULT_PROFILE,
    namespace: NamespaceOption = DEFAULT_NAMESPACE,
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    reverse: Annotated[bool, typer.Option("--reverse", help="Run reverse dependency tests.")] = False,
    no_ensure_cluster: Annotated[
        bool,
        typer.Option("--no-ensure-cluster", help="Do not create the kind cluster if missing."),
    ] = False,
    lint: Annotated[bool, typer.Option("--lint", help="Run helm lint before install.")] = False,
) -> None:
    service = KindTestService(root)
    service.run(
        KindTestOptions(
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
