"""Top-level Typer app wiring and small inline commands for chart-manager."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from chart_manager.cli import doctor as doctor_cli
from chart_manager.cli import events as events_cli
from chart_manager.cli import helmrelease as helmrelease_cli
from chart_manager.cli import output as output_mod
from chart_manager.cli import publish as publish_cli
from chart_manager.cli import upgrade as upgrade_cli
from chart_manager.cli import validate as validate_cli
from chart_manager.cli._wiring import container as _container
from chart_manager.cli._wiring import exit_if_failed as _exit_if_failed
from chart_manager.cli.streams import console, errors, narration, set_narration_quiet
from chart_manager.cli.streams import print_progress as _print_progress
from chart_manager.composition import Settings
from chart_manager.plumbing.errors import (
    ChartManagerError,
    ExternalCommandError,
    MissingToolError,
    SpecError,
)
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
from chart_manager.plumbing.logger import setup_logging
from chart_manager.services.chart_catalog import ChartCatalogEntry, ChartCatalogService
from chart_manager.services.chart_catalog_wire import catalog_to_dict, lifecycle_to_dict
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
from chart_manager.services.lifecycle.models import LifecyclePlan
from chart_manager.services.local_resources import (
    LocalTargetResolver,
    ResolvedChartTarget,
    ResolvedLocalTarget,
    ResolvedStackTarget,
    resolve_chart_target,
)
from chart_manager.settings import DEFAULT_CONFIG_FILE, set_config_file

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
# `<noun> <verb>` one level down: everything Grafana-specific this tool does
# today acts on a dashboard, and naming the noun leaves room for the things
# that are not dashboards (datasources, alert rules) to arrive as siblings
# rather than as more hyphenated verbs on the group itself.
grafana_dashboard_app = typer.Typer(
    no_args_is_help=True,
    help="Export and lint Grafana dashboard JSON.",
)

# The `event` group owns its own tree (group plus the `emit` subgroup), so it
# mounts onto the root like upgrade/publish.
events_cli.register(app)
# Root-level: a preflight is about the process, not about one group.
doctor_cli.register(app)
helmrelease_cli.register(helmrelease_app)
validate_cli.register_validate(chart_app)
validate_cli.register_cache(chart_cache_app)
publish_cli.register(chart_app)
upgrade_cli.register_upgrade(chart_app)
# Root-level and frozen: `renovate-global.json` pins its literal spelling.
upgrade_cli.register_finalize(app)

chart_app.add_typer(chart_cache_app, name="cache")
grafana_app.add_typer(grafana_dashboard_app, name="dashboard")

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
    #: and deliberately NOT seeded into `ctx.default_map`: seeding by
    #: parameter *name* would hand the global value to every parameter that
    #: happens to be called `output`, whatever it means there, and would
    #: erase the `None`-means-not-given distinction the resolver's precedence
    #: rests on. See `cli/output.py` for the full note.
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
    `parent.default_map[subcommand_name]`, so `grafana dashboard lint` needs
    `{"grafana": {"dashboard": {"lint": {"root": ...}}}}`. Returns None for a
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

    `-o` is a *format* everywhere on this surface, with no exception left:
    `grafana dashboard export` was the last command where it named a file,
    and that meaning moved to `--to` when the command was renamed. Writing to
    a path is always `--to`. The global still travels on `ctx.obj` rather
    than through `ctx.default_map` -- see `cli/output.py` for why the
    propagation that carries `--root` is the wrong mechanism for this one.

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

#: `chart list` and `chart show` speak the core projections minus `md`:
#: neither has a markdown form, and advertising one the resolver cannot
#: produce is the lie `cli/output.py` exists to prevent.
_CHART_CATALOG_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML)

ChartCatalogOutputOption = Annotated[
    str | None,
    output_mod.output_option(*_CHART_CATALOG_OUTPUTS),
]

#: The vocabulary a `--dry-run` plan is printed in. Same three projections;
#: named separately because the document is a *plan*, not a catalog, and the
#: two have no reason to stay equal.
_DRY_RUN_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML)

DryRunOutputOption = Annotated[
    str | None,
    output_mod.output_option(*_DRY_RUN_OUTPUTS, extra_help=" Requires --dry-run."),
]


@chart_app.command("list")
def list_charts(
    ctx: typer.Context,
    output: ChartCatalogOutputOption = None,
    root: RootOption = Path("."),
) -> None:
    """List Helm charts and their lifecycle capability status.

    `-o` defaults to `auto`: the table on a terminal, JSON in a pipe or in
    CI. The table was this command's only output for its whole life, which
    made `chart list | grep` a habit and the chart inventory unreadable to
    anything else; the JSON payload is the versioned document in
    `services/chart_catalog_wire.py`, so a second surface answers this
    question with the same bytes.
    """
    mode = output_mod.resolve(output, ctx, allowed=_CHART_CATALOG_OUTPUTS, console=console)
    entries = ChartCatalogService(root, charts_dir=Settings().charts_dir).list_entries()
    output_mod.emit(catalog_to_dict(entries), mode=mode, table=_catalog_table(entries))
    # A chart whose lifecycle document does not load is reported *in* the
    # projection (as `error`, in every format) and again as the exit code, so
    # neither a reader nor a pipeline has to learn the other's channel. What
    # failed is the *authoring* of a `Chart.yaml` or `chart-lifecycle.yaml`,
    # which is 6.1's spec error -- exit 3, not the generic 1 this used to
    # return.
    if any(entry.error is not None for entry in entries):
        raise typer.Exit(code=exit_code_for(Outcome.SPEC))


def _catalog_table(entries: Sequence[ChartCatalogEntry]) -> Table:
    """Render the chart catalog as the terminal projection."""
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
    for entry in entries:
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
    return table


def _document_table(document: dict[str, Any], *, title: str) -> Table:
    """Render a wire document as a Field/Value table over dotted paths.

    A flattening of the same document `-o json` emits rather than a
    hand-written layout, because `chart show`'s subject is the authored
    ChartLifecycle envelope and that schema grows a section at a time. A
    bespoke renderer would need editing every time the spec does, and until
    someone did it would silently omit whatever it had not been taught --
    which is the one thing a command called `show` must never do.
    """
    table = Table("Field", "Value", title=title)
    for field, value in _flatten(document):
        table.add_row(escape(field), escape(value))
    return table


def _flatten(value: Any, prefix: str = "") -> Iterator[tuple[str, str]]:
    """Walk a JSON-shaped document into (dotted path, rendered leaf) rows.

    A list of scalars stays on one row (`values: a.yaml, b.yaml`) because
    that is how it reads in the file it came from; a list of objects is
    indexed, because its members have structure worth addressing.
    """
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from _flatten(item, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, list) and value:
        if any(isinstance(item, dict | list) for item in value):
            for index, item in enumerate(value):
                yield from _flatten(item, f"{prefix}[{index}]")
        else:
            yield prefix, ", ".join(_leaf(item) for item in value)
    else:
        yield prefix, _leaf(value)


def _leaf(value: Any) -> str:
    """Render one leaf as JSON spells it, minus the quotes around strings.

    So a reader sees `true`/`null`/`{}` -- the tokens they would type back
    into the document -- and not Python's `True`/`None`/`{}`.
    """
    return value if isinstance(value, str) else json.dumps(value)


def _run_chart_test(
    chart: str,
    *,
    ctx: typer.Context,
    root: Path,
    profile: str,
    namespace: str | None,
    cluster_name: str,
    dependent_tests: bool,
    no_ensure_cluster: bool,
    lint: bool,
    dry_run: bool,
    output: str | None,
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
    request = EphemeralTestRequest(
        chart=target.name,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        ensure_cluster=not no_ensure_cluster,
        include_dependent_tests=dependent_tests,
        lint=lint,
    )
    if dry_run:
        _render_test_plan(service.plan(request), ctx=ctx, output=output)
        return
    service.run(request)


def _render_test_plan(plan: LifecyclePlan, *, ctx: typer.Context, output: str | None) -> None:
    """Print the compiled cluster-test plan; say on stderr what did not happen.

    The plan is what the caller asked for, so it is the projection and goes
    to stdout. That it was *only* a plan is narration, and stays off the
    stream a `-o json | jq` consumer reads.
    """
    mode = output_mod.resolve(output, ctx, allowed=_DRY_RUN_OUTPUTS, console=console)
    table = Table("Step", "Action", "Chart", "Profile", "Namespace", "Release")
    for step, action in enumerate(plan.actions, start=1):
        table.add_row(
            str(step),
            action.kind.value,
            action.target.chart,
            action.target.profile or "",
            action.target.namespace or "",
            action.target.release or "",
        )
    output_mod.emit(plan.to_dict(), mode=mode, table=table)
    for warning in plan.warnings:
        narration.print(f"[yellow]warn:[/yellow] {escape(warning)}")
    narration.print(
        "[yellow]dry run[/yellow]: no cluster was created, nothing was installed or tested"
    )


@chart_app.command("test")
def chart_test(
    ctx: typer.Context,
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
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the cluster-test plan and exit; create no cluster, install nothing.",
        ),
    ] = False,
    output: DryRunOutputOption = None,
) -> None:
    """Install and exercise one chart on an ephemeral local Kubernetes cluster.

    The chart is named positionally; `--chart` is the older spelling and is
    kept permanently, because `.github/workflows/ci.yaml` uses it and the
    flag costs nothing to keep once the positional exists.

    `--dry-run` prints the compiled lifecycle plan -- every namespace,
    install and helm test the run would perform, in order -- and exits 0
    having touched nothing. It is the same plan object the real run
    executes, so it cannot describe work that would not happen.

    `-o` selects the form that plan is printed in. It is only meaningful
    with `--dry-run` -- a real run's report is progress narration, not a
    document -- so naming it without `--dry-run` is a usage error rather
    than a flag that is quietly ignored.
    """
    if (chart_argument is None) == (chart is None):
        raise ChartManagerError("name exactly one chart, as the CHART argument or --chart")
    output_mod.require_dry_run(output, dry_run=dry_run)
    selected = chart_argument if chart_argument is not None else chart
    assert selected is not None
    _run_chart_test(
        selected,
        ctx=ctx,
        output=output,
        root=root,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        dependent_tests=dependent_tests,
        no_ensure_cluster=no_ensure_cluster,
        lint=lint,
        dry_run=dry_run,
    )


@chart_app.command("show")
def show_lifecycle(
    ctx: typer.Context,
    chart: str,
    output: ChartCatalogOutputOption = None,
    root: RootOption = Path("."),
) -> None:
    """Print one chart's normalized ChartLifecycle intent.

    `-o json`/`-o yaml` emit the authored envelope after normalization, so
    the output can be diffed against -- or pasted back into --
    `chart-lifecycle.yaml`. `-o table` flattens that same document onto
    dotted field paths for reading at a terminal, which is what `auto`
    selects there.
    """
    mode = output_mod.resolve(output, ctx, allowed=_CHART_CATALOG_OUTPUTS, console=console)
    document = lifecycle_to_dict(
        ChartCatalogService(root, charts_dir=Settings().charts_dir).get_lifecycle(chart)
    )
    output_mod.emit(
        document,
        mode=mode,
        table=_document_table(document, title=f"{chart} lifecycle"),
    )


#: Both dashboard commands speak the core vocabulary minus `md`: there is no
#: markdown projection of a dashboard document or of a lint report, and
#: `cli/output.py` offers `md` only where one exists.
_DASHBOARD_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML)

DashboardOutputOption = Annotated[
    str | None,
    output_mod.output_option(*_DASHBOARD_OUTPUTS),
]


@grafana_dashboard_app.command("export")
def grafana_dashboard_export(
    ctx: typer.Context,
    uid: Annotated[str, typer.Argument(help="Dashboard UID to export.")],
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    namespace: NamespaceOption = DEFAULT_NAMESPACE,
    release: Annotated[
        str,
        typer.Option(
            "--release", help="Grafana Helm release name (drives secret and service name)."
        ),
    ] = "grafana",
    to: Annotated[
        Path | None,
        typer.Option(
            "--to",
            help="Write the canonical dashboard JSON to this file. Missing parent directories are created. Default: the document goes to stdout.",
        ),
    ] = None,
    output: DashboardOutputOption = None,
) -> None:
    """Export a dashboard from a kind-deployed Grafana and normalize for git.

    Auth + connectivity are resolved from the cluster: the admin password is
    read from secret/<release>, then an ephemeral port-forward to svc/<release>
    carries the HTTP GET. No pre-existing port-forward required.

    `--to` is *where* the document goes; `-o` is what shape it takes. Until
    this command was renamed `-o` named the destination file, which is the one
    meaning it cannot keep now that every other command reads it as a format.
    The flip is deliberate and has no alias: a path handed to `-o` is a usage
    error that names `--to`, never a file written somewhere surprising.

    The file `--to` writes is always canonical JSON, whatever `-o` says --
    it is the git artifact, and a committed dashboard has exactly one legal
    byte sequence (`canonical_json`). `-o` therefore only decides what lands
    on stdout, and the document goes to exactly one place: with `--to` given,
    `json`/`yaml` print nothing and the file is the answer.

    `-o table` is a summary of what was exported rather than the document,
    because a dashboard is a deep tree with no table form and `auto` has to
    give a human at a terminal something readable. It prints in both cases,
    so an interactive `--to` export still says what it just wrote.
    """
    from chart_manager.services.grafana.dashboard_export import (
        ExportRequest,
        canonical_json,
        summarize_dashboard,
    )

    mode = output_mod.resolve(output, ctx, allowed=_DASHBOARD_OUTPUTS, console=console)
    # `fetch` rather than `export`: the normalized object is what the summary
    # and the yaml projection are derived from, and `canonical_json` is the
    # same function `export` would have applied.
    dashboard = (
        _container()
        .grafana_exporter()
        .fetch(
            ExportRequest(
                uid=uid,
                cluster_name=cluster_name,
                namespace=namespace,
                release=release,
            )
        )
    )
    if to is not None:
        try:
            to.parent.mkdir(parents=True, exist_ok=True)
            to.write_text(canonical_json(dashboard))
        except OSError as exc:
            # Same shape as `_changed_paths`: an OSError from a path the
            # caller typed into a flag is a usage error naming that flag, not
            # a traceback. A directory handed to `--to` was the traceback
            # case -- the write-side twin of the `--path DIR` lint defect.
            raise typer.BadParameter(
                f"cannot write dashboard to {to}: {exc}",
                param_hint="--to",
            ) from exc
        narration.print(f"[green]wrote[/green] {to}")

    if mode == output_mod.TABLE:
        summary = summarize_dashboard(dashboard)
        table = Table("UID", "Title", "Schema", "Top-level panels", "Datasource variables")
        table.add_row(
            escape(summary.uid),
            escape(summary.title),
            "-" if summary.schema_version is None else str(summary.schema_version),
            str(summary.top_level_panels),
            escape(", ".join(summary.datasource_variables)),
        )
        console.print(table)
    elif to is None:
        # `canonical_json` already ends in a newline, and neither projection
        # goes through `console`: Rich would wrap and highlight a document a
        # caller is piping into `jq` or committing.
        if mode == output_mod.JSON:
            typer.echo(canonical_json(dashboard), nl=False)
        else:
            output_mod.emit(dashboard, mode=output_mod.YAML)


@grafana_dashboard_app.command("lint")
def grafana_dashboard_lint(
    ctx: typer.Context,
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
    output: DashboardOutputOption = None,
) -> None:
    """Lint Grafana dashboards for repo-wide quality rules.

    `--path` accepts a file or a directory; a directory is linted
    recursively, which is what the default discovery already does.

    `-o table` is one greppable `path: [rule] message` line per finding --
    the shape a human scans and a CI log grep matches. `-o json`/`-o yaml`
    are the same report as the wire document owned by
    `services/grafana/wire.py`, which carries the tally as well as the
    findings so a consumer does not have to count lines.
    """
    from chart_manager.services.grafana.dashboard_lint import (
        discover_dashboards,
        expand_targets,
        lint_paths,
    )
    from chart_manager.services.grafana.wire import lint_result_to_dict

    mode = output_mod.resolve(output, ctx, allowed=_DASHBOARD_OUTPUTS, console=console)
    targets = (
        expand_targets(path)
        if path
        else discover_dashboards(root, charts_dir=Settings().charts_dir)
    )
    if not targets:
        # Linting nothing is not the same as linting clean. A wrong --root, a
        # renamed charts directory, and a --path directory holding no JSON all
        # land here, and exiting 0 made every one of them a silent CI pass.
        # `--allow-empty` is the explicit opt-out for a repo that genuinely
        # has no dashboards yet.
        narration.print("[yellow]no dashboards found[/yellow]")
        raise typer.Exit(code=exit_code_for(Outcome.SUCCESS if allow_empty else Outcome.FAILED))

    result = lint_paths(targets)
    # The findings are this command's report -- its data projection.
    if mode != output_mod.TABLE:
        output_mod.emit(lint_result_to_dict(result), mode=mode)
    else:
        # `typer.echo`, not `console.print`: a finding carries a rule id in
        # square brackets and a message quoting a PromQL expression, so Rich
        # would read `[R002-uid]` as markup and would wrap the long ones --
        # and a wrapped finding is no longer one greppable line.
        for finding in result.findings:
            typer.echo(finding.render())

    # The pass/fail tally narrates the run rather than reporting a finding.
    if not result.ok:
        narration.print(
            f"\n[red]{len(result.findings)} findings across "
            f"{result.files_with_findings}/{result.files_scanned} dashboards[/red]"
        )
        raise typer.Exit(code=exit_code_for(Outcome.FAILED))
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
        output_mod.emit(status_to_dict(status), mode=output)
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
        output_mod.emit(plan_to_dict(plan), mode=output)
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
        output_mod.emit(
            converge_to_dict(result, command=command, cluster_name=DEFAULT_CLUSTER_NAME),
            mode=output,
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
        output_mod.emit(action_to_dict(result, command=command), mode=output)
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
        callback=lambda value: output_mod.choice(value, _PLAN_KINDS, param_hint="--for"),
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
        if output == output_mod.TABLE:
            for chart in selected:
                console.print(chart)
        else:
            output_mod.emit(selected, mode=output)
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
    if output != output_mod.TABLE:
        output_mod.emit(result.to_dict(), mode=output)
    else:
        _render_impact_text(
            result,
            validation=for_ in {"validate", "all"},
            cluster_tests=for_ in {"test", "all"},
        )
    if result.spec_errors:
        # `spec_errors` is by construction a list of unparseable authored
        # lifecycle files, so this is design §6.1's spec error -- exit 3, not
        # the generic 1 it used to be. The impact document is still printed;
        # what failed is the input to it.
        raise typer.Exit(code=exit_code_for(Outcome.SPEC))


#: Which raised error means which outcome. Ordered most specific first --
#: `_outcome_for` returns on the first `isinstance` match -- so
#: `MissingToolError` has to precede the `ExternalCommandError` it subclasses,
#: and both have to precede the `ChartManagerError` catch-all that closes the
#: table and makes the lookup total.
#:
#: This is where design §6.1's rows 3, 4 and 127 come from: an unparseable
#: `chart-lifecycle.yaml` is not the same event as a helm that ran and
#: failed, which is not the same event as a helm that is not installed, and
#: before this every one of them exited 1 (except the absent binary, which
#: already had its own clause). A `CapabilityUnavailableError` deliberately
#: falls through to `FAILED`: asking a chart for a capability it has switched
#: off is not invalid configuration, so it is not a spec error.
_ERROR_OUTCOMES: tuple[tuple[type[ChartManagerError], Outcome], ...] = (
    (MissingToolError, Outcome.MISSING_BINARY),
    (ExternalCommandError, Outcome.TOOL),
    (SpecError, Outcome.SPEC),
    (ChartManagerError, Outcome.FAILED),
)


def _outcome_for(exc: ChartManagerError) -> Outcome:
    """Classify a domain error against `_ERROR_OUTCOMES`."""
    for error_type, outcome in _ERROR_OUTCOMES:
        if isinstance(exc, error_type):
            return outcome
    return Outcome.FAILED  # unreachable: the last row matches every subclass


def _os_error_text(exc: OSError) -> str:
    """A one-line reason for an OSError, naming the file when there is one.

    `str(OSError)` reads "[Errno 21] Is a directory: 'charts/'", which is a
    Python artifact; the operator wants the sentence without the errno.
    """
    if exc.strerror is None:
        return str(exc)
    return f"{exc.strerror.lower()}: {exc.filename}" if exc.filename else exc.strerror.lower()


def main() -> None:
    """Entry point: turn an escaped exception into a mapped exit code.

    Everything below writes one `error:` line and exits with a number from
    `plumbing/exit_codes.py`. Nothing may reach the operator as a traceback:
    a traceback is not a diagnostic to anyone who did not write this code,
    and it carries no exit code a pipeline can branch on.

    The two non-domain arms are ordered, and the order is the point.
    `FileNotFoundError` -- a data file the caller named is not there -- stays
    a plain failure (1), so a wrapper keying on 127 to say "install helm"
    does not fire for a missing values file. Every *other* `OSError` is the
    machine refusing rather than the run failing (a directory where a file
    was expected, a permission denial, a refused connection -- `socket`
    errors are `OSError` too), which is design §6.1's environment error, 5.
    """
    try:
        settings = Settings()
        setup_logging(settings.log_level, fmt=settings.log_format)
        app()
    except ChartManagerError as exc:
        errors.print(f"[red]error:[/red] {escape(str(exc))}")
        sys.exit(exit_code_for(_outcome_for(exc)))
    except FileNotFoundError as exc:
        errors.print(f"[red]error:[/red] file not found: {escape(str(exc.filename or exc))}")
        sys.exit(exit_code_for(Outcome.FAILED))
    except OSError as exc:
        errors.print(f"[red]error:[/red] {escape(_os_error_text(exc))}")
        sys.exit(exit_code_for(Outcome.ENVIRONMENT))
