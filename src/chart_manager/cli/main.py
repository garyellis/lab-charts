"""Top-level Typer app wiring and small inline commands for chart-manager."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.markup import escape
from rich.table import Table

from chart_manager.cli import events as events_cli
from chart_manager.cli import helmrelease as helmrelease_cli
from chart_manager.cli import output as output_mod
from chart_manager.cli import publish as publish_cli
from chart_manager.cli import upgrade as upgrade_cli
from chart_manager.cli import validate as validate_cli
from chart_manager.cli.streams import (
    data_console,
    error_console,
    narration_console,
    set_narration_quiet,
)
from chart_manager.composition import Container, Settings
from chart_manager.plumbing.errors import ChartManagerError, MissingToolError
from chart_manager.plumbing.logger import setup_logging
from chart_manager.services.chart_catalog import ChartCatalogService
from chart_manager.services.ci import MatrixSelection
from chart_manager.services.ci_wire import cluster_test_matrix_to_dict
from chart_manager.services.clusters.development import (
    LAB_CA_SECRET_NAME,
    LAB_CA_SECRET_NAMESPACE,
    DevelopmentClusterAccessHints,
    DevelopmentClusterActionResult,
    DevelopmentClusterPlan,
    DevelopmentClusterResult,
    DevelopmentClusterStatus,
    action_to_dict,
    converge_to_dict,
    plan_to_dict,
    status_to_dict,
)
from chart_manager.services.clusters.ephemeral import (
    DEFAULT_CLUSTER_NAME,
    DEFAULT_NAMESPACE,
    DEFAULT_PROFILE,
    EphemeralTestRequest,
)
from chart_manager.services.lifecycle.impact import LifecycleImpactService
from chart_manager.services.local_resources import (
    LocalTargetResolver,
    ResolvedChartTarget,
    ResolvedLocalTarget,
    ResolvedStackTarget,
    resolve_chart_target,
)
from chart_manager.services.progress import ProgressEvent
from chart_manager.settings import DEFAULT_CONFIG_FILE, set_config_file

#: The selected `--output` projection -- tables, listings, JSON documents.
#: Goes to stdout, because that is what a caller pipes or captures.
console = data_console()
#: Everything the caller did not ask for as output -- progress, hints,
#: warnings. Goes to stderr so it can never corrupt `console`.
#: See `cli/streams.py` for the rule.
narration = narration_console()
#: Terminal error reporting. Same stream as `narration`, separate console
#: because `-q` silences narration and must not silence the reason a command
#: failed -- a quiet run that dies with no output is unsupportable.
#: `error_console()` rather than `narration_console()` is what makes that
#: structural: only narration consoles are registered with
#: `streams.set_narration_quiet`, which `-q` and `--output json` both drive.
errors = error_console()


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
    narration.print(text)


def _exit_if_failed(ok: bool) -> None:
    """The surface's single exit-code rule: a not-ok result is exit 1.

    Services report partial failure on the result object rather than by
    raising, so a surface that only renders it reports success for a run in
    which charts failed.
    """
    if not ok:
        raise typer.Exit(1)


app = typer.Typer(no_args_is_help=True, help="Local and CI workflows for lab Helm charts.")
chart_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect, validate, test, publish, and upgrade Helm charts.",
)
chart_cache_app = typer.Typer(
    no_args_is_help=True,
    help="Manage chart-manager's on-disk render artifacts.",
)
local_app = typer.Typer(
    no_args_is_help=True,
    help="Create, inspect, stop, and reset local Kubernetes chart development environments.",
)
helmrelease_app = typer.Typer(
    no_args_is_help=True,
    help="Operate on Flux HelmRelease resources in a separate GitOps repo.",
)
# Grafana-specific subcommands. Anything that knows about Grafana JSON / API
# conventions lives here, not under the generic `chart` group.
grafana_app = typer.Typer(no_args_is_help=True, help="Grafana-specific tooling.")

# The `event` group owns its own tree (group plus the `emit` subgroup), so it
# mounts onto the root like upgrade/publish.
events_cli.register(app)
helmrelease_cli.register(helmrelease_app)
validate_cli.register_validate(chart_app)
validate_cli.register_cache(chart_cache_app)
publish_cli.register(chart_app)
upgrade_cli.register_upgrade(chart_app)
# Root-level and frozen: `renovate-global.json` pins its literal spelling.
upgrade_cli.register_finalize(app)

chart_app.add_typer(chart_cache_app, name="cache")

app.add_typer(chart_app, name="chart")
app.add_typer(local_app, name="local")
app.add_typer(grafana_app, name="grafana")
app.add_typer(helmrelease_app, name="helmrelease")


@dataclass(frozen=True)
class GlobalOptions:
    """The resolved global options for one invocation.

    Stashed on `ctx.obj` so a command can read what the caller asked for
    globally without re-deriving it. `root` is deliberately *not* read from
    here by commands -- it reaches them through Click's `default_map` as the
    fallback for their own `--root`, so an explicit per-command `--root`
    still wins.
    """

    root: Path
    config: Path
    quiet: bool
    verbosity: int
    no_color: bool
    #: The invocation-wide `-o`. Read by `cli/output.resolve` via `ctx.obj`
    #: and deliberately NOT seeded into `ctx.default_map`: `grafana
    #: export-dashboard` has a `Path`-typed parameter also named `output`,
    #: and seeding by name would redirect the dashboard into a file called
    #: `json`. See `cli/output.py` for the full note.
    output: str


def _package_version() -> str:
    """Return the installed distribution version.

    `PackageNotFoundError` means chart_manager is on `sys.path` without being
    installed -- a source tree run directly. Say so rather than inventing a
    number a bug report would then quote.
    """
    try:
        return metadata.version("chart-manager")
    except metadata.PackageNotFoundError:
        return "unknown (not installed as a distribution)"


def _root_default_map(command: Any, root: Path) -> dict[str, Any] | None:
    """Nested Click `default_map` handing `root` to every command that takes it.

    Click looks a parameter up in this order: command line, environment,
    `default_map`, declared default. Seeding `default_map` therefore makes the
    global `--root` a *fallback* -- the 18 per-command `--root` flags keep
    overriding it, which is the whole point of landing this without touching
    them.

    Nested rather than flat because Click hands each subcommand
    `parent.default_map[subcommand_name]`, so `grafana lint-dashboards` needs
    `{"grafana": {"lint-dashboards": {"root": ...}}}`. Returns None for a
    branch with nothing to configure, so empty groups are pruned rather than
    contributing `{}`.

    Typed against `Any`: typer 0.26 vendors Click as `typer._click`, so there
    is no importable `click.Command` to annotate against, and reaching into a
    vendored module from the surface would be worse than this.
    """
    subcommands: dict[str, Any] | None = getattr(command, "commands", None)
    if subcommands is None:
        has_root = any(param.name == "root" for param in command.params)
        return {"root": root} if has_root else None
    nested = {
        name: mapping
        for name, sub in subcommands.items()
        if (mapping := _root_default_map(sub, root)) is not None
    }
    return nested or None


@app.callback()
def global_options(
    ctx: typer.Context,
    root: Annotated[
        Path | None,
        typer.Option(
            "--root",
            help="Repository root for every command. Also CHART_MANAGER_ROOT, or `root:` in the config file. A command's own --root still wins.",
        ),
    ] = None,
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            help="YAML config file. Absent is fine; every setting has a default.",
        ),
    ] = DEFAULT_CONFIG_FILE,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress narration. Data and errors still print."),
    ] = False,
    verbose: Annotated[
        int,
        typer.Option("--verbose", "-v", count=True, help="Repeatable. -v enables debug logging."),
    ] = 0,
    no_color: Annotated[
        bool,
        typer.Option("--no-color", help="Disable color. The NO_COLOR environment variable does the same."),
    ] = False,
    output: output_mod.GlobalOutputOption = output_mod.AUTO,
) -> None:
    """Local and CI workflows for lab Helm charts.

    `-o/--output` sets the default projection for whichever command runs.
    A command's own `-o` still wins, so `chart-manager -o json plan -o table`
    prints a table. Commands that have no projection ignore it.

    It does **not** collide with `grafana export-dashboard -o PATH`, which
    still writes a file: Click scopes options per command, so the root's `-o`
    and a subcommand's `-o` are separate parameters, and this callback
    deliberately does not propagate `output` through `ctx.default_map` the
    way it propagates `root`. See `cli/output.py`. That flag flips meaning in
    P2.2, on purpose and with no alias.

    Deliberately absent, and not an oversight:

    * **No global `--version` flag.** `--version` already means the *chart*
      version on `event emit build/promote`, `chart publish`, and all three
      `helmrelease` commands. One flag, two meanings by position, is a bad
      flag -- so the CLI's own version is the `version` command (8.6).
    """
    # Order matters: the config file must be located before anything reads
    # Settings, because Settings is where the config file's values enter.
    set_config_file(config)
    settings = Settings()

    # `flag > CHART_MANAGER_ROOT > config.yaml > default`. The first step is
    # here because Settings never sees argv; the rest is Settings' source
    # ordering. Settings is frozen and is not written back to.
    resolved_root = root if root is not None else settings.root

    # NO_COLOR is a convention, not a value: the spec says any non-empty
    # value disables color.
    disable_color = no_color or bool(os.environ.get("NO_COLOR"))
    for sink in (console, narration, errors):
        sink.no_color = disable_color
    # Only narration is silenced. `console` carries the projection the caller
    # asked for and `errors` carries why it failed; `-q` must not swallow
    # either, or `-q` becomes indistinguishable from `2>/dev/null`.
    #
    # Process-wide rather than `narration.quiet = quiet`: `cli/validate.py`,
    # `cli/publish.py` and `cli/deprecation.py` hold their own narration
    # consoles, so assigning only to this module's left `-q` a no-op for most
    # of the surface's output.
    set_narration_quiet(quiet)

    if verbose:
        setup_logging("DEBUG", fmt=settings.log_format)

    ctx.obj = GlobalOptions(
        root=resolved_root,
        config=config,
        quiet=quiet,
        verbosity=verbose,
        no_color=disable_color,
        output=output,
    )
    ctx.default_map = _root_default_map(ctx.command, resolved_root)


@app.command("version")
def version_command() -> None:
    """Print the chart-manager version."""
    console.print(_package_version())


RootOption = Annotated[Path, typer.Option("--root", help="Repository root.")]
ProfileOption = Annotated[str, typer.Option("--profile", help="Cluster-test profile.")]
ClusterNameOption = Annotated[str, typer.Option("--cluster-name", help="kind cluster name.")]
NamespaceOption = Annotated[
    str,
    typer.Option("--namespace", help="Kubernetes namespace."),
]
NamespaceOverrideOption = Annotated[
    str | None,
    typer.Option(
        "--namespace",
        help="Override the namespace declared by the selected ChartLifecycle profile.",
    ),
]

def _choice(value: str, allowed: tuple[str, ...], option: str) -> str:
    if value not in allowed:
        raise typer.BadParameter(
            f"unknown value: {value} (allowed: {', '.join(allowed)})",
            param_hint=option,
        )
    return value


def _emit_json(data: dict[str, Any]) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True))


def _emit_yaml(data: dict[str, Any]) -> None:
    typer.echo(yaml.safe_dump(data, sort_keys=False), nl=False)


@chart_app.command("list")
def list_charts(root: RootOption = Path(".")) -> None:
    """List Helm charts and their lifecycle capability status."""
    service = ChartCatalogService(root, charts_dir=Settings().charts_dir)
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


def _run_chart_test(
    chart: str,
    *,
    root: Path,
    profile: str,
    namespace: str | None,
    cluster_name: str,
    dependent_tests: bool,
    no_ensure_cluster: bool,
    lint: bool,
) -> None:
    """Run one chart test through the canonical charts command."""
    root = root.resolve()
    target = _resolve_chart_target(root, chart)
    charts_dir = target.path.parent.relative_to(root)
    service = _container().ephemeral_test_cluster_service(
        root,
        progress=_print_progress,
        charts_dir=charts_dir,
    )
    service.run(
        EphemeralTestRequest(
            chart=target.name,
            profile=profile,
            namespace=namespace,
            cluster_name=cluster_name,
            ensure_cluster=not no_ensure_cluster,
            include_dependent_tests=dependent_tests,
            lint=lint,
        )
    )


@chart_app.command("test")
def chart_test(
    chart_argument: Annotated[
        str | None,
        typer.Argument(metavar="[CHART]", help="Chart name or chart directory."),
    ] = None,
    chart: Annotated[
        str | None,
        typer.Option(
            "--chart",
            help="Chart name or chart directory. Retained alongside the CHART argument.",
        ),
    ] = None,
    root: RootOption = Path("."),
    profile: ProfileOption = DEFAULT_PROFILE,
    namespace: NamespaceOverrideOption = None,
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
        typer.Option("--no-ensure-cluster", help="Do not create the test cluster if missing."),
    ] = False,
    lint: Annotated[bool, typer.Option("--lint", help="Run helm lint before install.")] = False,
) -> None:
    """Install and exercise one chart on an ephemeral local Kubernetes cluster.

    The chart is named positionally; `--chart` is the older spelling and is
    kept permanently, because `.github/workflows/ci.yaml` uses it and the
    flag costs nothing to keep once the positional exists.
    """
    if (chart_argument is None) == (chart is None):
        raise ChartManagerError("name exactly one chart, as the CHART argument or --chart")
    selected = chart_argument if chart_argument is not None else chart
    assert selected is not None
    _run_chart_test(
        selected,
        root=root,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        dependent_tests=dependent_tests,
        no_ensure_cluster=no_ensure_cluster,
        lint=lint,
    )


@chart_app.command("show")
def show_lifecycle(chart: str, root: RootOption = Path(".")) -> None:
    """Print one chart's normalized ChartLifecycle intent."""
    lifecycle = ChartCatalogService(
        root,
        charts_dir=Settings().charts_dir,
    ).get_lifecycle(chart)
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
        narration.print(f"[green]wrote[/green] {output}")


@grafana_app.command("lint-dashboards")
def grafana_lint_dashboards(
    root: RootOption = Path("."),
    path: Annotated[
        list[Path],
        typer.Option(
            "--path",
            help="Specific dashboard JSON file (repeatable). Default: all under the configured chart directory's grafana-dashboards/dashboards/.",
        ),
    ] = [],
    allow_empty: Annotated[
        bool,
        typer.Option(
            "--allow-empty",
            help="Treat 'no dashboards found' as success. For repos or paths that legitimately have no dashboards yet.",
        ),
    ] = False,
) -> None:
    """Lint Grafana dashboards for repo-wide quality rules."""
    from chart_manager.services.grafana.dashboard_lint import discover_dashboards, lint_paths

    targets = (
        list(path)
        if path
        else discover_dashboards(root, charts_dir=Settings().charts_dir)
    )
    if not targets:
        # Linting nothing is not the same as linting clean. A wrong --root, a
        # renamed charts directory, or a --path that matches no file all land
        # here, and exiting 0 made every one of them a silent CI pass. Exit 1
        # per the exit-code table: the thing you asked about did not succeed.
        # `--allow-empty` is the explicit opt-out for a repo that genuinely
        # has no dashboards yet.
        narration.print("[yellow]no dashboards found[/yellow]")
        raise typer.Exit(0 if allow_empty else 1)

    result = lint_paths(targets)
    # The findings are this command's report -- its data projection.
    for finding in result.findings:
        console.print(finding.render())

    # The pass/fail tally narrates the run rather than reporting a finding.
    if not result.ok:
        narration.print(
            f"\n[red]{len(result.findings)} findings across "
            f"{result.files_with_findings}/{result.files_scanned} dashboards[/red]"
        )
        raise typer.Exit(1)
    narration.print(f"[green]ok[/green]: {result.files_scanned} dashboards passed")


def _resolve_local_target(root: Path, target: str) -> ResolvedLocalTarget:
    """Resolve a chart directory or LocalStack through configured repository paths."""
    return LocalTargetResolver(root, local_config=Settings().local_config).resolve(target)


def _resolve_chart_target(root: Path, chart: str) -> ResolvedChartTarget:
    """Resolve either a configured chart name or an explicit chart directory."""
    settings = Settings()
    return resolve_chart_target(
        root,
        chart,
        charts_dir=settings.charts_dir,
        local_config=settings.local_config,
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
        return _resolve_chart_target(root, chart)
    assert stack is not None
    return _resolve_stack_target(root, stack)


def _validate_local_profile(target: ResolvedLocalTarget, profile: str | None) -> None:
    if profile is not None and isinstance(target, ResolvedStackTarget):
        raise ChartManagerError(
            "--profile is only valid for a chart target; LocalStack releases "
            "declare their own profiles"
        )


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


def _emit_document(data: dict[str, Any], output: str) -> None:
    """Write a wire payload in the resolved machine projection.

    Only reached once `resolve()` has narrowed the mode, so `yaml` is the
    only alternative to `json` and a third arm would be unreachable.
    """
    if output == output_mod.JSON:
        _emit_json(data)
    else:
        _emit_yaml(data)


@local_app.command("up")
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
    output: LocalOutputOption = None,
    root: RootOption = Path("."),
) -> None:
    """Create or start a local cluster and converge the chart or stack.

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
    resolved = _resolve_local_selection(root.resolve(), chart=chart, stack=stack)
    _validate_local_profile(resolved, profile)
    service = _container().development_cluster_service(root, progress=_print_progress)
    if dry_run:
        _render_plan(
            service.plan_target(resolved, profile=profile, cluster_name=DEFAULT_CLUSTER_NAME),
            output,
        )
        return
    result = service.up_target(
        resolved,
        profile=profile,
        cluster_name=DEFAULT_CLUSTER_NAME,
        skip_installed=skip_installed,
    )
    _render_development_cluster_result(result, output, command="up")
    _exit_if_failed(result.ok)


@local_app.command("down")
def local_down(
    ctx: typer.Context,
    dry_run: DryRunOption = False,
    output: LocalOutputOption = None,
    root: RootOption = Path("."),
) -> None:
    """Stop the configured local cluster while preserving its state.

    Installed Helm releases, PVCs, and provider-owned caches survive. Use
    `local up` to bring it back. Any active port-forward for the
    environment is stopped with it.

    """
    output = output_mod.resolve(output, ctx, allowed=_LOCAL_OUTPUTS, console=console)
    service = _container().development_cluster_service(root, progress=_print_progress)
    if dry_run:
        _render_plan(service.plan_down(DEFAULT_CLUSTER_NAME), output)
        return
    _render_cluster_action(
        service.down(DEFAULT_CLUSTER_NAME),
        output,
        command="down",
        verb="stopped",
        absent="not running",
    )


@local_app.command("reset")
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
    output: LocalOutputOption = None,
    root: RootOption = Path("."),
) -> None:
    """Destroy and recreate a local cluster, then converge the chart or stack.

    `--dry-run` prints the same plan `local up --dry-run` would, marked as
    destructive: reset resolves everything first and only then deletes, so
    the plan a dry run shows is exactly the work the real run would do
    after the delete.
    """
    output = output_mod.resolve(output, ctx, allowed=_LOCAL_OUTPUTS, console=console)
    resolved = _resolve_local_selection(root.resolve(), chart=chart, stack=stack)
    _validate_local_profile(resolved, profile)
    service = _container().development_cluster_service(root, progress=_print_progress)
    if dry_run:
        _render_plan(
            service.plan_target(
                resolved,
                profile=profile,
                cluster_name=DEFAULT_CLUSTER_NAME,
                destroys=True,
            ),
            output,
        )
        return
    result = service.reset_target(
        resolved,
        profile=profile,
        cluster_name=DEFAULT_CLUSTER_NAME,
    )
    _render_development_cluster_result(result, output, command="reset")
    _exit_if_failed(result.ok)


@local_app.command("status")
def local_status(
    ctx: typer.Context,
    output: LocalOutputOption = None,
    root: RootOption = Path("."),
) -> None:
    """Report the local cluster: whether it exists, its releases, and its URLs.

    A read, not a grade. The cluster being absent, unreachable, or full of
    failed releases is *the answer* and still exits 0 -- the caller decides
    what counts as bad, which is what makes the documented idiom work:

        chart-manager local status -o json | jq '.releases[] | select(.status!="deployed")'

    Every lookup is the one the converge path already makes: `helm list -A`
    is the same snapshot `local up` uses to skip installed releases, and the
    URLs are the same VirtualService hosts it prints when it finishes.
    """
    output = output_mod.resolve(output, ctx, allowed=_LOCAL_OUTPUTS, console=console)
    status = (
        _container()
        .development_cluster_service(root, progress=_print_progress)
        .status(DEFAULT_CLUSTER_NAME)
    )
    if output != output_mod.TABLE:
        _emit_document(status_to_dict(status), output)
        return
    _render_status_table(status)


def _render_status_table(status: DevelopmentClusterStatus) -> None:
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
    if status.port_forward_pid is not None:
        console.print(f"  port-forward: pid {status.port_forward_pid}")

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


def _render_plan(plan: DevelopmentClusterPlan, output: str) -> None:
    """Print a resolved `--dry-run` plan and change nothing.

    The plan is the projection, so it lands on stdout in every mode; the
    "nothing was changed" reassurance is narration, because a caller piping
    the plan into a file wants the plan and not the disclaimer.
    """
    if output != output_mod.TABLE:
        _emit_document(plan_to_dict(plan), output)
        return
    title = f"Dry run: local {plan.command}"
    if plan.target is not None:
        title += f" -> {plan.target} ({plan.target_kind})"
    console.print(f"[bold]{escape(title)}[/bold]  cluster={escape(plan.cluster_name)}")
    if plan.destroys:
        console.print("  [yellow]would destroy and recreate the cluster first[/yellow]")
    if plan.entries:
        table = Table("Source", "Chart", "Profile", "Namespace", title="Would install")
        for entry in plan.entries:
            table.add_row(entry.source, entry.chart, entry.profile, entry.namespace)
        console.print(table)
    narration.print("[dim]dry run: nothing was changed[/dim]")


def _render_development_cluster_result(
    result: DevelopmentClusterResult,
    output: str,
    *,
    command: str,
) -> None:
    """Print the converge summary, then the access hints.

    Order is operational and load-bearing: what happened, then how to reach
    it. Everything here is pure formatting -- every decision (which bucket,
    whether the CA hint applies, which URLs exist) was already made by
    DevelopmentClusterService and arrives on the result.

    The access hints print in every mode, because they are narration on
    stderr in every mode. They are advice for an operator, not part of the
    document -- see `services/clusters/development/wire.py` for why the
    payload does not carry them.
    """
    if output == output_mod.TABLE:
        table = Table("Status", "Chart", "Profile", "Namespace", title="Lab install summary")
        for entry in result.applied:
            table.add_row("[green]applied[/green]", entry.chart, entry.profile, entry.namespace)
        for entry in result.no_change:
            table.add_row("[dim]no-change[/dim]", entry.chart, entry.profile, entry.namespace)
        for failed in result.failed:
            table.add_row("[red]failed[/red]", failed.chart, failed.profile, failed.namespace)
        # The summary table is the result projection; the rest narrates.
        console.print(table)
    else:
        _emit_document(
            converge_to_dict(result, command=command, cluster_name=DEFAULT_CLUSTER_NAME),
            output,
        )
    if not result.ok:
        narration.print(
            f"[red]{len(result.failed)} chart(s) failed[/red]; see diagnostics above"
        )
    _render_access_hints(result.hints)


def _render_access_hints(hints: DevelopmentClusterAccessHints) -> None:
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
    for url in hints.urls:
        narration.print(f"  {url}")
        if url != hints.grafana_url:
            continue
        if hints.grafana_error is not None:
            narration.print(
                f"    [yellow]could not read admin password:[/yellow] {hints.grafana_error}"
            )
        elif hints.grafana_credentials is not None:
            user, password = hints.grafana_credentials
            narration.print(f"    user: {user}\n    pass: {password}")


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
    result: DevelopmentClusterActionResult,
    output: str,
    *,
    command: str,
    verb: str,
    absent: str,
) -> None:
    """Print the outcome of ``down`` plus any port-forward we reaped.

    The human form is a mutation status line rather than a document, so it
    narrates onto stderr as it always did. `-o json`/`-o yaml` do produce a
    document -- `changed` is the one bit a script cannot recover afterwards,
    since a cluster that was already stopped and one this call stopped look
    identical a moment later.
    """
    if output != output_mod.TABLE:
        _emit_document(action_to_dict(result, command=command), output)
        return
    state = verb if result.changed else absent
    narration.print(f"local cluster {state}: {result.cluster_name}")
    if result.port_forward_pid is not None:
        narration.print(f"stopped port-forward (pid {result.port_forward_pid})")


def _impact_service(root: Path) -> LifecycleImpactService:
    """Build the impact service at a seam tests can replace."""
    settings = Settings()
    return LifecycleImpactService(
        root,
        charts_dir=settings.charts_dir,
        local_config=settings.local_config,
    )


def _changed_paths(
    changed_files: Path | None,
    changed_file: list[str],
) -> list[str]:
    changes: list[str] = []
    if changed_files is not None:
        try:
            contents = changed_files.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise typer.BadParameter(
                f"cannot read changed-files input {changed_files}: {exc}",
                param_hint="--changed-files",
            ) from exc
        changes.extend(line.strip() for line in contents.splitlines() if line.strip())
    changes.extend(path.strip() for path in changed_file if path.strip())
    if not changes:
        raise typer.BadParameter(
            "provide at least one changed path via --changed-files or --changed-file",
            param_hint="--changed-files / --changed-file",
        )
    return changes


def _render_impact_text(
    result: Any,
    *,
    validation: bool = True,
    cluster_tests: bool = True,
) -> None:
    """Render the impact document as text, optionally narrowed to one section.

    `validation`/`cluster_tests` implement `plan --for`. Warnings and spec
    errors are printed for every selection deliberately: a warning is usually
    the answer to "why is nothing selected?", which is the question a narrowed
    view is most often asked.
    """
    if validation:
        typer.echo("Validation:")
        if not result.validation:
            typer.echo("  none")
        for case in result.validation:
            typer.echo(f"  {case.chart}/{case.environment}")
            for reason in case.reasons:
                typer.echo(
                    f"    - {reason.code}: {reason.changed_file.as_posix()} — {reason.detail}"
                )

    if cluster_tests:
        typer.echo("Cluster tests:")
        if not result.cluster_tests:
            typer.echo("  none")
        for case in result.cluster_tests:
            typer.echo(f"  {case.chart}/{case.profile}")
            for reason in case.reasons:
                typer.echo(
                    f"    - {reason.code}: {reason.changed_file.as_posix()} — {reason.detail}"
                )

    if result.warnings:
        typer.echo("Warnings:")
        for warning in result.warnings:
            typer.echo(f"  - {warning}")
    if result.spec_errors:
        typer.echo("Spec errors:")
        for error in result.spec_errors:
            typer.echo(f"  - {error}")


#: `plan`'s output vocabulary: the core three plus its own `github`, the
#: GitHub Actions matrix document. `github` stays command-local because it is
#: meaningless for `chart list` or `local status` -- see `cli/output.py`.
_PLAN_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML, output_mod.GITHUB)

#: The kinds of work a plan can cover. `all` is the default because the
#: question the command answers -- "given a change set, what work is
#: selected?" -- is normally asked about the whole pipeline.
_PLAN_KINDS = ("validate", "test", "publish", "all")

PlanOutputOption = Annotated[
    str | None,
    output_mod.output_option(*_PLAN_OUTPUTS, extra_help=" github is the GHA matrix JSON."),
]

PlanForOption = Annotated[
    str,
    typer.Option(
        "--for",
        help="Work kind to plan: validate, test, publish, or all.",
        callback=lambda value: _choice(value, _PLAN_KINDS, "--for"),
    ),
]


def _plan_cluster_tests(
    root: Path,
    *,
    base: str,
    all_charts: bool,
    charts: list[str] | None,
    changed_files: Path | None,
    changed_file: list[str],
) -> tuple[Any, ...]:
    """Select cluster-test entries from whichever change source was given.

    Two engines, because the two change sources are genuinely different
    questions and each already has a service that answers it:

      * explicit paths (`--changed-files`/`--changed-file`) are what the
        lifecycle impact service analyses, and its `cluster_tests` are the
        same `ClusterTestImpact` values the matrix is built from;
      * `--base`/`--all`/`--chart` is a `MatrixSelection`, and
        `CiService.matrix` owns the `all` > `--chart` > diff precedence.

    Neither branch decides *which charts* anything -- that stays in
    `services/`. This function only decides which service was asked.
    """
    if changed_files is not None or changed_file:
        paths = _changed_paths(changed_files, changed_file)
        return tuple(_impact_service(root).analyze(paths).cluster_tests)
    selection = MatrixSelection(
        base=base,
        all_charts=all_charts,
        charts=tuple(charts or ()),
    )
    return tuple(_container().ci_service(root).matrix(selection))


@app.command("plan")
def plan(
    ctx: typer.Context,
    base: Annotated[str, typer.Option("--base", help="Git comparison base.")] = "origin/main",
    changed_files: Annotated[
        Path | None,
        typer.Option(
            "--changed-files",
            help="Path to a newline-delimited changed-file list.",
        ),
    ] = None,
    changed_file: Annotated[
        list[str],
        typer.Option(
            "--changed-file",
            help="Changed repository-relative path (repeatable).",
        ),
    ] = [],
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
    for_: PlanForOption = "all",
    output: PlanOutputOption = None,
    root: RootOption = Path("."),
) -> None:
    """Answer "given a change set, what work is selected?".

    One command with output projections rather than one command per consumer.
    `-o github` is the GitHub Actions matrix a workflow feeds to
    `strategy.matrix`; `-o table` is the same selection with the *reasons*
    kept, which is what answers "why is this chart in my matrix?" and, via
    warnings, "why is nothing selected?"; `-o json`/`-o yaml` are the machine
    document.

    `--for` narrows the work kind. It selects the engine for `publish` (a
    lexical ownership projection that must not inherit lifecycle fanout) and
    narrows the `table` view for `validate`/`test`. `-o json`/`-o yaml`
    deliberately emit the *complete* impact document whatever `--for` says:
    the payload is a versioned wire contract owned by `services/`, and
    deleting a key from it here would fork that contract in the surface.

    The output default is `auto`, so a bare `plan` in a pipe emits `json`
    rather than the table a terminal gets. `--for publish -o table` is the
    newline-delimited chart list `.github/workflows/ci.yaml` captures, and it
    names `-o table` explicitly for exactly that reason.
    """
    output = output_mod.resolve(output, ctx, allowed=_PLAN_OUTPUTS, console=console)
    if output == "github" and for_ in {"validate", "publish"}:
        raise typer.BadParameter(
            f"-o github is the cluster-test matrix, which has no '{for_}' projection",
            param_hint="--for",
        )
    # Flag exclusivity is the one part of this that is genuinely the
    # surface's: it classifies how the caller was *invoked*. Which selector
    # runs, and what shape the answer takes, belong to services/.
    if all_charts and charts:
        raise ChartManagerError("--all and --chart are mutually exclusive")

    if for_ == "publish":
        # Direct ownership only, from an explicit list. `--changed-file` is
        # not accepted because the service reads the file itself, and
        # publishing must not inherit lifecycle or dependency fanout.
        if changed_files is None:
            raise typer.BadParameter(
                "planning publish work needs an explicit changed-file list",
                param_hint="--changed-files",
            )
        selected = _container().ci_service(root).directly_changed_charts(changed_files)
        if output == "json":
            typer.echo(json.dumps(selected, indent=2))
        elif output == "yaml":
            typer.echo(yaml.safe_dump(selected, sort_keys=False), nl=False)
        else:
            for chart in selected:
                console.print(chart)
        return

    if output == "github":
        entries = _plan_cluster_tests(
            root,
            base=base,
            all_charts=all_charts,
            charts=charts,
            changed_files=changed_files,
            changed_file=changed_file,
        )
        payload = cluster_test_matrix_to_dict(entries)
        # Compact separators and sorted keys because this lands in a shell
        # variable in `.github/workflows/ci.yaml`, not in a human's terminal.
        typer.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return

    # table/json/yaml are projections of the impact document, which only the
    # lifecycle impact service produces, and only from explicit paths.
    # `_changed_paths` raises the "provide at least one changed path" usage
    # error when neither source was given.
    result = _impact_service(root).analyze(_changed_paths(changed_files, changed_file))
    data = result.to_dict()
    if output == "json":
        _emit_json(data)
    elif output == "yaml":
        _emit_yaml(data)
    else:
        _render_impact_text(
            result,
            validation=for_ in {"validate", "all"},
            cluster_tests=for_ in {"test", "all"},
        )
    if result.spec_errors:
        raise typer.Exit(1)


def main() -> None:
    """Entry point: map domain errors to exit 1 and an absent tool to 127.

    MissingToolError is caught first because it subclasses ChartManagerError.
    127 is the shell's "command not found", so it is reserved for a genuinely
    absent binary -- a wrapper keying on it to say "install helm" must not
    fire because a *data* file was missing, which is what catching bare
    FileNotFoundError here used to do.
    """
    try:
        settings = Settings()
        setup_logging(settings.log_level, fmt=settings.log_format)
        app()
    except MissingToolError as exc:
        errors.print(f"[red]error:[/red] {escape(str(exc))}")
        sys.exit(127)
    except ChartManagerError as exc:
        errors.print(f"[red]error:[/red] {escape(str(exc))}")
        sys.exit(1)
    except FileNotFoundError as exc:
        errors.print(f"[red]error:[/red] file not found: {escape(str(exc.filename or exc))}")
        sys.exit(1)
