"""The composition root: the command tree in `--help` order, the global options,
and the one place an escaped exception becomes an exit code.

Holds no command implementation beyond `version`. Each command package exposes
plain Typer callbacks (and the `event` sub-app) from its `cli.py`; this file
mounts them, so the shape of the whole surface reads as one screen.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from chart_manager.cli import output as output_mod
from chart_manager.cli._container import start_invocation
from chart_manager.cli.streams import console, errors, narration, set_narration_quiet
from chart_manager.commands.catalog import cli as catalog_cli
from chart_manager.commands.doctor import cli as doctor_cli
from chart_manager.commands.events import cli as events_cli
from chart_manager.commands.grafana import cli as grafana_cli
from chart_manager.commands.local import cli as local_cli
from chart_manager.commands.plan import cli as plan_cli
from chart_manager.commands.promote import cli as promote_cli
from chart_manager.commands.publish import cli as publish_cli
from chart_manager.commands.test import cli as test_cli
from chart_manager.commands.upgrade import cli as upgrade_cli
from chart_manager.commands.validate import cli as validate_cli
from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaError,
    KubeconformSchemaLockError,
    KubeconformSchemaRenderError,
    KubeconformSchemaSourceEnvironmentError,
    KubeconformSchemaSourceError,
)
from chart_manager.plumbing.errors import (
    ChartManagerError,
    ExternalCommandError,
    MissingToolError,
    SpecError,
    WorkspaceNotFoundError,
)
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
from chart_manager.plumbing.logger import setup_logging
from chart_manager.settings import DEFAULT_CONFIG_FILE, load_settings, set_config_file

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
promote_app = typer.Typer(
    no_args_is_help=True,
    help="Operate on Flux HelmRelease resources in a separate GitOps repo.",
)
grafana_app = typer.Typer(no_args_is_help=True, help="Grafana-specific tooling.")
grafana_dashboard_app = typer.Typer(
    no_args_is_help=True,
    help="Export and lint Grafana dashboard JSON.",
)
schemas_app = typer.Typer(
    no_args_is_help=True,
    help="Synchronize and inspect the locked Kubernetes schema store.",
)


# --- global options --------------------------------------------------------


@dataclass(frozen=True)
class GlobalOptions:
    """The resolved global options for one invocation, stashed on `ctx.obj`."""

    config: Path
    quiet: bool
    verbosity: int
    no_color: bool
    #: The invocation-wide `-o`, read by `cli/output.resolve`. Not put in
    #: `ctx.default_map`, which would hand it to every parameter named `output`.
    output: str


@app.callback()
def global_options(
    ctx: typer.Context,
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
        typer.Option(
            "--no-color", help="Disable color. The NO_COLOR environment variable does the same."
        ),
    ] = False,
    output: output_mod.GlobalOutputOption = output_mod.AUTO,
) -> None:
    """Local and CI workflows for lab Helm charts.

    `-o/--output` sets the default projection for whichever command runs.
    A command's own `-o` still wins, so `chart-manager -o json plan -o table`
    prints a table. Commands that have no projection ignore it.

    There is no global `--version` flag: `--version` means the chart version
    on `chart publish` and `promote`, so the CLI's own is the `version` command.
    """
    # The config file must be set before anything reads Settings.
    set_config_file(config)
    settings = start_invocation().settings

    # NO_COLOR is a convention, not a value: the spec says any non-empty
    # value disables color.
    disable_color = no_color or bool(os.environ.get("NO_COLOR"))
    # The surface's three shared consoles (`cli/streams.py`).
    for sink in (console, narration, errors):
        sink.no_color = disable_color
    # Process-wide, so it also reaches per-invocation narration consoles.
    set_narration_quiet(quiet)

    if verbose:
        setup_logging("DEBUG", fmt=settings.log_format)

    ctx.obj = GlobalOptions(
        config=config,
        quiet=quiet,
        verbosity=verbose,
        no_color=disable_color,
        output=output,
    )


# --- the one command that belongs to the app itself ------------------------


def _package_version() -> str:
    """Return the installed distribution version, or say it is not installed."""
    try:
        return metadata.version("chart-manager")
    except metadata.PackageNotFoundError:
        return "unknown (not installed as a distribution)"


def version_command() -> None:
    """Print the chart-manager version."""
    console.print(_package_version())


# --- wiring ----------------------------------------------------------------
#
# Read as the `--help` listing. Typer lists a group's commands in the order
# they were added, then its sub-groups in the order they were mounted.

app.command("doctor")(doctor_cli.doctor)
# Hidden and frozen: `renovate-global.json` pins its literal spelling.
app.command("upgrade-finalize", hidden=True)(upgrade_cli.upgrade_finalize)
app.command("version")(version_command)
app.command("plan")(plan_cli.plan)

chart_app.command("validate")(validate_cli.validate)
chart_app.command("publish")(publish_cli.publish)
chart_app.command("upgrade")(upgrade_cli.upgrade)
chart_app.command("list")(catalog_cli.list_charts)
chart_app.command("test")(test_cli.chart_test)
chart_app.command("teardown")(test_cli.chart_teardown)
chart_app.command("show")(catalog_cli.show_lifecycle)
chart_cache_app.command("clean")(validate_cli.clean)
chart_app.add_typer(chart_cache_app, name="cache")

local_app.command("up")(local_cli.local_up)
local_app.command("down")(local_cli.local_down)
local_app.command("reset")(local_cli.local_reset)
local_app.command("status")(local_cli.local_status)

grafana_dashboard_app.command("export")(grafana_cli.grafana_dashboard_export)
grafana_dashboard_app.command("lint")(grafana_cli.grafana_dashboard_lint)
grafana_app.add_typer(grafana_dashboard_app, name="dashboard")

promote_app.command("pr")(promote_cli.pr)
promote_app.command("monitor")(promote_cli.monitor)
promote_app.command("test")(promote_cli.test)

schemas_app.command("sync")(validate_cli.sync)

app.add_typer(events_cli.event_app, name="event")
app.add_typer(chart_app, name="chart")
app.add_typer(local_app, name="local")
app.add_typer(grafana_app, name="grafana")
app.add_typer(promote_app, name="promote")
app.add_typer(schemas_app, name="schemas")


# --- errors become exit codes ----------------------------------------------

#: Which raised error means which outcome. Ordered most specific first, since
#: `_outcome_for` returns on the first `isinstance` match (`MissingToolError`
#: must come before `ExternalCommandError`, its parent class); the
#: `ChartManagerError` catch-all closes the table. A `CapabilityUnavailableError`
#: falls through to `FAILED`: it is not a spec error.
_ERROR_OUTCOMES: tuple[tuple[type[ChartManagerError], Outcome], ...] = (
    (MissingToolError, Outcome.MISSING_BINARY),
    (ExternalCommandError, Outcome.TOOL),
    (KubeconformSchemaSourceEnvironmentError, Outcome.ENVIRONMENT),
    (KubeconformSchemaSourceError, Outcome.TOOL),
    (KubeconformSchemaConfigurationError, Outcome.SPEC),
    (KubeconformSchemaLockError, Outcome.SPEC),
    (KubeconformSchemaError, Outcome.TOOL),
    (SpecError, Outcome.SPEC),
    (WorkspaceNotFoundError, Outcome.ENVIRONMENT),
    (ChartManagerError, Outcome.FAILED),
)


def _outcome_for(exc: ChartManagerError) -> Outcome:
    """Classify a domain error against `_ERROR_OUTCOMES`."""
    if isinstance(exc, KubeconformSchemaRenderError):
        return exc.outcome
    for error_type, outcome in _ERROR_OUTCOMES:
        if isinstance(exc, error_type):
            return outcome
    return Outcome.FAILED  # unreachable: the last row matches every subclass


def _os_error_text(exc: OSError) -> str:
    """A one-line reason for an OSError without the errno, naming the file if any."""
    if exc.strerror is None:
        return str(exc)
    return f"{exc.strerror.lower()}: {exc.filename}" if exc.filename else exc.strerror.lower()


def main() -> None:
    """Entry point: turn an escaped exception into a mapped exit code.

    `FileNotFoundError` must precede `OSError`: a missing file the caller
    named is a plain failure, while any other `OSError` is an environment error.
    """
    try:
        settings = load_settings()
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
