from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from lab_charts.plumbing.errors import LabChartsError
from lab_charts.plumbing.spec import CheckSpec
from lab_charts.services.charts import ChartService
from lab_charts.services.ci import CiService
from lab_charts.services.dependencies import DependencyService
from lab_charts.services.kind_test import KindTestOptions, KindTestService

console = Console()

app = typer.Typer(no_args_is_help=True, help="Local and CI workflows for lab Helm charts.")
charts_app = typer.Typer(no_args_is_help=True, help="Inspect charts and test specs.")
deps_app = typer.Typer(no_args_is_help=True, help="Resolve test dependencies.")
kind_app = typer.Typer(no_args_is_help=True, help="Run kind-backed chart tests.")
ci_app = typer.Typer(no_args_is_help=True, help="CI-oriented helpers.")

app.add_typer(charts_app, name="charts")
app.add_typer(deps_app, name="deps")
app.add_typer(kind_app, name="kind")
app.add_typer(ci_app, name="ci")

RootOption = Annotated[Path, typer.Option("--root", help="Repository root.")]
ProfileOption = Annotated[str, typer.Option("--profile", help="test-spec profile.")]


@charts_app.command("list")
def list_charts(root: RootOption = Path(".")) -> None:
    service = ChartService(root)
    table = Table("Chart", "Version", "Dependencies", "Profiles")
    for name in service.list_charts():
        try:
            chart = service.get_chart(name)
        except LabChartsError:
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


@deps_app.command("plan")
def dependency_plan(
    chart: str,
    root: RootOption = Path("."),
    profile: ProfileOption = "minimal",
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
    profile: ProfileOption = "minimal",
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
    cluster_name: Annotated[str, typer.Option("--cluster-name", help="kind cluster name.")] = "lab-charts",
    root: RootOption = Path("."),
) -> None:
    service = KindTestService(root)
    service.kind.ensure_cluster(cluster_name)
    console.print(f"kind cluster ready: {cluster_name}")


def _expose_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "lab-charts" / "expose"


def _expose_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@kind_app.command("expose")
def kind_expose(
    cluster_name: Annotated[str, typer.Option("--cluster-name", help="kind cluster name.")] = "lab-charts",
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
    state_dir = _expose_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"{cluster_name}.json"
    log_file = state_dir / f"{cluster_name}.log"

    if stop:
        if not state_file.exists():
            console.print(f"no port-forward state for cluster [bold]{cluster_name}[/bold]")
            return
        state = json.loads(state_file.read_text())
        pid = state.get("pid")
        if pid and _expose_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                console.print(f"stopped port-forward (pid {pid})")
            except ProcessLookupError:
                console.print(f"process {pid} already gone")
        else:
            console.print(f"process {pid} not running; clearing state")
        state_file.unlink(missing_ok=True)
        return

    if state_file.exists():
        existing = json.loads(state_file.read_text())
        if _expose_alive(existing.get("pid", -1)):
            console.print(
                f"[red]port-forward already running[/red] for cluster [bold]{cluster_name}[/bold] "
                f"(pid {existing['pid']}). Stop it first: "
                f"lab-charts kind expose --cluster-name {cluster_name} --stop"
            )
            raise typer.Exit(1)
        state_file.unlink()

    ports = list(port) if port else ["8443:443", "8080:80"]
    if "/" not in service:
        console.print(f"[red]--service must be namespace/name, got: {service}[/red]")
        raise typer.Exit(2)
    namespace, name = service.split("/", 1)
    context = f"kind-{cluster_name}"

    args = [
        "kubectl",
        "--context",
        context,
        "port-forward",
        "-n",
        namespace,
        f"svc/{name}",
        *ports,
    ]

    log_handle = log_file.open("w")
    try:
        proc = subprocess.Popen(
            args,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        log_handle.close()
        console.print("[red]kubectl not found on PATH[/red]")
        raise typer.Exit(127)

    # Give kubectl a moment to bind the ports or fail.
    time.sleep(1.0)
    if proc.poll() is not None:
        log_handle.close()
        console.print(
            f"[red]port-forward exited immediately (rc={proc.returncode})[/red]\n"
            f"{log_file.read_text().strip()}"
        )
        raise typer.Exit(1)

    state = {
        "cluster": cluster_name,
        "service": service,
        "ports": ports,
        "pid": proc.pid,
        "log": str(log_file),
    }
    state_file.write_text(json.dumps(state, indent=2))

    console.print(f"[bold]port-forward running[/bold] (pid {proc.pid})  cluster={cluster_name}  service={service}")
    for mapping in ports:
        local, remote = mapping.split(":", 1)
        scheme = "https" if remote in {"443", "8443"} else "http"
        console.print(f"  {scheme}://*.kind.local:{local}/  ->  {service}:{remote}")
    console.print(f"  log:  {log_file}")
    console.print(f"  stop: lab-charts kind expose --cluster-name {cluster_name} --stop")


@kind_app.command("test")
def kind_test(
    chart: str,
    root: RootOption = Path("."),
    profile: ProfileOption = "minimal",
    namespace: Annotated[str, typer.Option("--namespace", help="Kubernetes namespace.")] = "observability",
    cluster_name: Annotated[str, typer.Option("--cluster-name", help="kind cluster name.")] = "lab-charts",
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
    profile: ProfileOption = "minimal",
    namespace: Annotated[str, typer.Option("--namespace", help="Kubernetes namespace.")] = "observability",
) -> None:
    CiService(root).install_source_chart(chart, profile, namespace)


@ci_app.command("upgrade")
def ci_upgrade(
    chart: str,
    oci_ref: Annotated[str, typer.Option("--from-oci", help="OCI chart ref for the main-branch artifact.")],
    root: RootOption = Path("."),
    profile: ProfileOption = "minimal",
    namespace: Annotated[str, typer.Option("--namespace", help="Kubernetes namespace.")] = "observability",
) -> None:
    CiService(root).upgrade_from_oci(chart, profile, namespace, oci_ref)


def main() -> None:
    try:
        app()
    except LabChartsError as exc:
        console.print(f"[red]error:[/red] {exc}")
        sys.exit(1)
