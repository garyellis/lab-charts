"""`local up/down/reset/status`: flags, target selection and rendering for the dev cluster.

Plans and status reports are the projection (stdout); access hints and dry-run notes
are narration (stderr).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from chart_manager.cli import output as output_mod
from chart_manager.cli._container import container as _container
from chart_manager.cli._container import exit_if_failed as _exit_if_failed
from chart_manager.cli._container import repository_root
from chart_manager.cli._options import (
    ProvisionHooksOption,
    provision_hooks_enabled,
)
from chart_manager.cli.streams import console, narration
from chart_manager.cli.streams import print_progress as _print_progress
from chart_manager.commands.local import run as local_run
from chart_manager.commands.local.access import LAB_CA_SECRET_NAME, LAB_CA_SECRET_NAMESPACE
from chart_manager.commands.local.models import (
    DevClusterAccessHints,
    DevClusterActionResult,
    DevClusterPlan,
    DevClusterResult,
    DevClusterStatus,
)
from chart_manager.commands.local.targets import (
    LocalTargetResolver,
    ResolvedLocalTarget,
    ResolvedStackTarget,
)
from chart_manager.commands.local.wire import (
    action_to_dict,
    converge_to_dict,
    plan_to_dict,
    status_to_dict,
)
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.settings import DEFAULT_CLUSTER_NAME
from chart_manager.shared.charts.chart import resolve_chart_target

#: `local`'s output vocabulary. No `md`: a cluster snapshot has no markdown
#: projection, and offering one that silently rendered as a table would be
#: the "silently different answer" `cli/output.py` refuses.
_LOCAL_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML)

LocalOutputOption = Annotated[str | None, output_mod.output_option(*_LOCAL_OUTPUTS)]

DryRunOption = Annotated[
    bool,
    typer.Option(
        "--dry-run",
        help="Resolve and print the plan in --output form, then exit 0. Changes nothing.",
    ),
]


def _resolve_local_target(root: Path, target: str) -> ResolvedLocalTarget:
    """Resolve a chart directory or LocalStack through configured repository paths."""
    workspace = _container().workspace(root)
    return LocalTargetResolver(workspace.root, local_config=workspace.spec.local_cluster).resolve(
        target
    )


def _resolve_stack_target(root: Path, stack: str) -> ResolvedStackTarget:
    resolved = _resolve_local_target(root, stack)
    if not isinstance(resolved, ResolvedStackTarget):
        raise ChartManagerError(f"--stack must select a LocalStack, not {resolved.kind}")
    return resolved


def _resolve_local_selection(
    root: Path,
    *,
    chart: str | None,
    stack: str | None,
) -> ResolvedLocalTarget:
    if (chart is None) == (stack is None):
        raise ChartManagerError("select exactly one of --chart or --stack")
    if chart is not None:
        return resolve_chart_target(_container().workspace(root), chart)
    assert stack is not None
    return _resolve_stack_target(root, stack)


def _validate_local_profile(target: ResolvedLocalTarget, profile: str | None) -> None:
    if profile is not None and isinstance(target, ResolvedStackTarget):
        raise ChartManagerError(
            "--profile is only valid for a chart target; LocalStack releases "
            "declare their own profiles"
        )


def local_up(
    ctx: typer.Context,
    chart: Annotated[
        str | None,
        typer.Option("--chart", help="Chart name or chart directory."),
    ] = None,
    stack: Annotated[
        str | None,
        typer.Option("--stack", help="Named LocalStack or LocalStack YAML file."),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help=("Profile for a single chart. Authored stack files declare profiles per release."),
        ),
    ] = None,
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
    dry_run: DryRunOption = False,
    run_provision_hooks: ProvisionHooksOption = None,
    output: LocalOutputOption = None,
) -> None:
    """Create or start the dev cluster and converge the chart or stack.

    Works whether the environment is missing, stopped, or already running.
    Continue-on-error: a failing chart is reported in the summary but does
    not abort the run.

    Default: converge -- every chart in the install plan runs `helm
    upgrade --install`, helm itself no-ops the ones whose rendered
    manifests haven't changed. This is the helmfile/Argo workflow and
    picks up values-file edits on re-run. Pass `--skip-installed` to avoid
    invoking Helm for releases already in `helm list -A`.

    `--dry-run` runs the same preflight the real converge runs first --
    LocalCluster, bootstrap, and the target's install plan are all resolved
    -- and prints what would be installed without touching Kind, Helm, or
    the apiserver.
    """
    output = output_mod.resolve(output, ctx, allowed=_LOCAL_OUTPUTS, console=console)
    root = repository_root()
    resolved = _resolve_local_selection(root.resolve(), chart=chart, stack=stack)
    _validate_local_profile(resolved, profile)
    container = _container()
    hooks_enabled = provision_hooks_enabled(run_provision_hooks, ci=container.settings.ci)
    workspace = container.workspace(root)
    if dry_run:
        _render_plan(
            local_run.plan(
                resolved,
                workspace=workspace,
                profile=profile,
                run_hooks=hooks_enabled,
                progress=_print_progress,
            ),
            output,
        )
        return
    result = local_run.up(
        resolved,
        workspace=workspace,
        runner=container.command_runner(),
        settings=container.settings,
        profile=profile,
        skip_installed=skip_installed,
        run_hooks=hooks_enabled,
        progress=_print_progress,
    )
    _render_dev_cluster_result(result, output, command="up")
    _exit_if_failed(result.ok)


def local_down(
    ctx: typer.Context,
    dry_run: DryRunOption = False,
    output: LocalOutputOption = None,
) -> None:
    """Stop the dev cluster while preserving its state.

    Installed Helm releases, PVCs, and provider-owned caches survive. Use
    `local up` to bring it back.

    """
    output = output_mod.resolve(output, ctx, allowed=_LOCAL_OUTPUTS, console=console)
    if dry_run:
        _render_plan(local_run.plan_down(), output)
        return
    container = _container()
    _render_cluster_action(
        local_run.down(
            runner=container.command_runner(), settings=container.settings, progress=_print_progress
        ),
        output,
        command="down",
        verb="stopped",
        absent="not running",
    )


def local_reset(
    ctx: typer.Context,
    chart: Annotated[
        str | None,
        typer.Option("--chart", help="Chart name or chart directory."),
    ] = None,
    stack: Annotated[
        str | None,
        typer.Option("--stack", help="Named LocalStack or LocalStack YAML file."),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help=("Profile for a single chart. Authored stack files declare profiles per release."),
        ),
    ] = None,
    dry_run: DryRunOption = False,
    run_provision_hooks: ProvisionHooksOption = None,
    output: LocalOutputOption = None,
) -> None:
    """Destroy and recreate the dev cluster, then converge the chart or stack.

    `--dry-run` prints the same plan `local up --dry-run` would, marked as
    destructive: reset resolves everything first and only then deletes, so
    the plan a dry run shows is exactly the work the real run would do
    after the delete.
    """
    output = output_mod.resolve(output, ctx, allowed=_LOCAL_OUTPUTS, console=console)
    root = repository_root()
    resolved = _resolve_local_selection(root.resolve(), chart=chart, stack=stack)
    _validate_local_profile(resolved, profile)
    container = _container()
    hooks_enabled = provision_hooks_enabled(run_provision_hooks, ci=container.settings.ci)
    workspace = container.workspace(root)
    if dry_run:
        _render_plan(
            local_run.plan(
                resolved,
                workspace=workspace,
                profile=profile,
                destroys=True,
                run_hooks=hooks_enabled,
                progress=_print_progress,
            ),
            output,
        )
        return
    result = local_run.reset(
        resolved,
        workspace=workspace,
        runner=container.command_runner(),
        settings=container.settings,
        profile=profile,
        run_hooks=hooks_enabled,
        progress=_print_progress,
    )
    _render_dev_cluster_result(result, output, command="reset")
    _exit_if_failed(result.ok)


def local_status(
    ctx: typer.Context,
    output: LocalOutputOption = None,
) -> None:
    """Report the dev cluster: whether it exists, its releases, and its URLs.

    A read, not a grade. The cluster being absent, unreachable, or full of
    failed releases is *the answer* and still exits 0 -- the caller decides
    what counts as bad, which is what makes the documented idiom work:

        chart-manager local status -o json | jq '.releases[] | select(.status!="deployed")'

    Every lookup is the one the converge path already makes: `helm list -A`
    is the same snapshot `local up` uses to skip installed releases, and the
    URLs are the same VirtualService hosts it prints when it finishes.
    """
    output = output_mod.resolve(output, ctx, allowed=_LOCAL_OUTPUTS, console=console)
    container = _container()
    status = local_run.status(
        workspace=container.workspace(), runner=container.command_runner(), settings=container.settings
    )
    if output != output_mod.TABLE:
        output_mod.emit(status_to_dict(status), mode=output)
        return
    _render_status_table(status)


def _render_status_table(status: DevClusterStatus) -> None:
    """Print the human projection of a cluster snapshot.

    All of it lands on stdout, unlike the converge commands' access hints:
    here the state *is* the result, so a caller who redirects stderr must
    still get the whole report.
    """
    state = "[green]running[/green]" if status.exists else "[yellow]absent[/yellow]"
    console.print(f"cluster [bold]{escape(status.cluster_name)}[/bold]: {state}")
    if not status.exists:
        return
    console.print(f"  context: {escape(status.context or '')} ({escape(status.provider or '')})")

    if status.releases_error is not None:
        console.print(f"  [yellow]{escape(status.releases_error)}[/yellow]")
    elif not status.releases:
        console.print("  no helm releases installed")
    else:
        table = Table("Namespace", "Release", "Revision", "Status", title="Helm releases")
        for release in status.releases:
            table.add_row(
                release.namespace,
                release.name,
                str(release.revision),
                release.status,
            )
        console.print(table)

    if status.urls_error is not None:
        console.print(f"  [yellow]{escape(status.urls_error)}[/yellow]")
    elif status.urls:
        console.print("\n[bold]URLs:[/bold]")
        for url in status.urls:
            console.print(f"  {url}")

    if status.drift.error is not None:
        console.print(f"  [yellow]drift check skipped: {escape(status.drift.error)}[/yellow]")
    elif status.drift.drifted:
        console.print(
            f"  [yellow]port mapping drift[/yellow]: kind-config declares host ports "
            f"{list(status.drift.missing)} that the running cluster does not publish; "
            "'chart-manager local reset' applies them"
        )


def _render_plan(plan: DevClusterPlan, output: str) -> None:
    """Print a resolved `--dry-run` plan and change nothing.

    The plan is the projection, so it lands on stdout in every mode; the
    "nothing was changed" reassurance is narration, because a caller piping
    the plan into a file wants the plan and not the disclaimer.
    """
    if output != output_mod.TABLE:
        output_mod.emit(plan_to_dict(plan), mode=output)
        return
    title = f"Dry run: local {plan.command}"
    if plan.target is not None:
        title += f" -> {plan.target} ({plan.target_kind})"
    console.print(f"[bold]{escape(title)}[/bold]  cluster={escape(plan.cluster_name)}")
    if plan.destroys:
        console.print("  [yellow]would destroy and recreate the cluster first[/yellow]")
    state = "enabled" if plan.provisioning_hooks_enabled else "disabled"
    console.print(f"  provisioning hooks: {state}")
    for phase, argv in plan.provisioning_hooks:
        console.print(f"    {escape(phase)}: {escape(' '.join(argv))}")
    if plan.entries:
        table = Table("Source", "Chart", "Profile", "Namespace", title="Would install")
        for entry in plan.entries:
            table.add_row(entry.source, entry.chart, entry.profile, entry.namespace)
        console.print(table)
    narration.print("[dim]dry run: nothing was changed[/dim]")


def _render_dev_cluster_result(
    result: DevClusterResult,
    output: str,
    *,
    command: str,
) -> None:
    """Print the converge summary, then the access hints.

    Order is operational and load-bearing: what happened, then how to reach
    it. Everything here is pure formatting -- every decision (which bucket,
    whether the CA hint applies, which URLs exist) was already made by
    commands/local/run.py and arrives on the result.

    The access hints print in every mode, because they are narration on
    stderr in every mode. They are advice for an operator, not part of the
    document -- see `commands/local/wire.py` for why the
    payload does not carry them.
    """
    if output == output_mod.TABLE:
        table = Table("Status", "Chart", "Profile", "Namespace", title="Dev cluster install summary")
        for entry in result.applied:
            table.add_row("[green]applied[/green]", entry.chart, entry.profile, entry.namespace)
        for entry in result.no_change:
            table.add_row("[dim]no-change[/dim]", entry.chart, entry.profile, entry.namespace)
        for failed in result.failed:
            table.add_row("[red]failed[/red]", failed.chart, failed.profile, failed.namespace)
        # The summary table is the result projection; the rest narrates.
        console.print(table)
    else:
        output_mod.emit(
            converge_to_dict(result, command=command, cluster_name=DEFAULT_CLUSTER_NAME),
            mode=output,
        )
    if not result.ok:
        narration.print(f"[red]{len(result.failed)} chart(s) failed[/red]; see diagnostics above")
    _render_access_hints(result.hints)


def _render_access_hints(hints: DevClusterAccessHints) -> None:
    """Print the CA-trust block and the URL block for a finished converge.

    All of this is narration: it is advice for the operator about how to
    reach what was just installed, not the result of the command. It goes
    to stderr so `local up` can grow an `--output json` projection later
    without these blocks landing in the payload.
    """
    if hints.ca_trust_hint:
        _print_ca_import_hint()
    if hints.urls_error is not None:
        narration.print(f"[yellow]warn:[/yellow] {hints.urls_error}")
        return
    if not hints.urls:
        return
    narration.print("\n[bold]URLs:[/bold]")
    credentials = {entry.url: entry for entry in hints.credentials}
    for url in hints.urls:
        narration.print(f"  {url}")
        entry = credentials.get(url)
        if entry is None:
            continue
        if entry.error is not None:
            narration.print(f"    [yellow]could not read credentials:[/yellow] {entry.error}")
        else:
            narration.print(f"    user: {entry.username}\n    pass: {entry.password}")


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
    narration.print("\n[bold]Trust the lab CA[/bold] (one-time, per workstation):")
    narration.print(f"  [dim]{cmd}[/dim]")
    narration.print("  Then import ~/lab-ca.crt into your OS keychain and mark it trusted.")
    if sys.platform == "darwin":
        macos_trust = (
            "security add-trusted-cert -d -r trustRoot "
            "-k ~/Library/Keychains/login.keychain-db ~/lab-ca.crt"
        )
        narration.print(f"  [dim]macOS one-liner: {macos_trust}[/dim]")
    narration.print(
        "  [dim]Re-import after every 'local reset' -- the lab CA is "
        "regenerated each fresh install.[/dim]"
    )
    narration.print(
        "  [dim]Firefox users: also set network.dns.localDomains = "
        '"localhost" in about:config[/dim]'
    )
    narration.print(
        "  [dim]Optional for curl/k6: "
        'echo "127.0.0.1 grafana.localhost" | sudo tee -a /etc/hosts[/dim]'
    )


def _render_cluster_action(
    result: DevClusterActionResult,
    output: str,
    *,
    command: str,
    verb: str,
    absent: str,
) -> None:
    """Print the outcome of ``down``.

    The human form is a mutation status line rather than a document, so it
    narrates onto stderr as it always did. `-o json`/`-o yaml` do produce a
    document -- `changed` is the one bit a script cannot recover afterwards,
    since a cluster that was already stopped and one this call stopped look
    identical a moment later.
    """
    if output != output_mod.TABLE:
        output_mod.emit(action_to_dict(result, command=command), mode=output)
        return
    state = verb if result.changed else absent
    narration.print(f"dev cluster {state}: {result.cluster_name}")
