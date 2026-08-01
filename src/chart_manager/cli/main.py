"""Assembly of the chart-manager CLI: the command tree, the global options,
and the one place an escaped exception becomes an exit code.

Deliberately holds no command implementation beyond `version`. Every group
lives in its own module and exposes `register()`; this file decides what the
tree looks like and nothing about what any command does. That is not
tidiness -- it is what keeps the *shape* of the surface reviewable as a
single screen, and it is the property that was lost while four groups
(`chart`, `local`, `grafana`, `plan`) were inlined here and this file grew
past 1600 lines.

Registration order is `--help` order, so the wiring block at the bottom is
read top to bottom as the listing a user sees.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape

from chart_manager.cli import chart as chart_cli
from chart_manager.cli import doctor as doctor_cli
from chart_manager.cli import events as events_cli
from chart_manager.cli import grafana as grafana_cli
from chart_manager.cli import helmrelease as helmrelease_cli
from chart_manager.cli import local as local_cli
from chart_manager.cli import output as output_mod
from chart_manager.cli import plan as plan_cli
from chart_manager.cli import publish as publish_cli
from chart_manager.cli import upgrade as upgrade_cli
from chart_manager.cli import validate as validate_cli
from chart_manager.cli.streams import console, errors, narration, set_narration_quiet
from chart_manager.composition import Settings
from chart_manager.plumbing.errors import (
    ChartManagerError,
    ExternalCommandError,
    MissingToolError,
    SpecError,
)
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
from chart_manager.plumbing.logger import setup_logging
from chart_manager.settings import DEFAULT_CONFIG_FILE, set_config_file

# --- the command tree ------------------------------------------------------

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


# --- global options --------------------------------------------------------


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
    # These are the whole surface's three consoles, not this module's: every
    # `cli/` module imports them from `cli/streams.py`, so setting the flag
    # here reaches `chart validate` and `chart publish` too. It did not while
    # each module derived its own.
    for sink in (console, narration, errors):
        sink.no_color = disable_color
    # Only narration is silenced. `console` carries the projection the caller
    # asked for and `errors` carries why it failed; `-q` must not swallow
    # either, or `-q` becomes indistinguishable from `2>/dev/null`.
    #
    # Process-wide rather than `narration.quiet = quiet`: `cli/helmrelease.py`
    # builds a narration console per invocation, so assigning only to the
    # shared one would leave `-q` a no-op there.
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


# --- the one command that belongs to the app itself ------------------------


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


def version_command() -> None:
    """Print the chart-manager version."""
    console.print(_package_version())


# --- wiring ----------------------------------------------------------------
#
# Read as the `--help` listing: Typer lists commands in registration order,
# then groups in the order they were mounted.

# The `event` group owns its own tree (group plus the `emit` subgroup), so it
# mounts onto the root like upgrade/publish.
events_cli.register(app)
# Root-level: a preflight is about the process, not about one group.
doctor_cli.register(app)
# Root-level and frozen: `renovate-global.json` pins its literal spelling.
upgrade_cli.register_finalize(app)
app.command("version")(version_command)
# Root-level: `plan` is asked about the repository, not about one chart.
plan_cli.register(app)

validate_cli.register_validate(chart_app)
validate_cli.register_cache(chart_cache_app)
publish_cli.register(chart_app)
upgrade_cli.register_upgrade(chart_app)
chart_cli.register(chart_app)

local_cli.register(local_app)
grafana_cli.register(grafana_dashboard_app)
helmrelease_cli.register(helmrelease_app)

chart_app.add_typer(chart_cache_app, name="cache")
grafana_app.add_typer(grafana_dashboard_app, name="dashboard")

app.add_typer(chart_app, name="chart")
app.add_typer(local_app, name="local")
app.add_typer(grafana_app, name="grafana")
app.add_typer(helmrelease_app, name="helmrelease")


# --- errors become exit codes ----------------------------------------------

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
