"""`chart upgrade` and the hidden `upgrade-finalize`: flags and rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from chart_manager.cli import output as output_mod
from chart_manager.cli._container import container as _container
from chart_manager.cli.streams import console
from chart_manager.commands.upgrade import finalize
from chart_manager.commands.upgrade.finalize import load_update_data, reject_symlinks
from chart_manager.commands.upgrade.models import (
    FinalizeRequest,
    FinalizeResult,
    UpgradeRequest,
    UpgradeResult,
)
from chart_manager.commands.upgrade.run import run
from chart_manager.plumbing.documents import to_document
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.shared.charts.chart import chart_target, resolve_chart_target

_CALLBACK_DATA_ENV = "RENOVATE_POST_UPGRADE_COMMAND_DATA_FILE"

_OUTPUTS = (output_mod.TABLE, output_mod.JSON)

OutputOption = Annotated[str | None, output_mod.output_option(*_OUTPUTS)]


def upgrade(
    ctx: typer.Context,
    chart: Annotated[
        str, typer.Argument(help="Chart name or repository-relative chart path.")
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Discover and plan without pushing or opening a PR."),
    ] = False,
    output: OutputOption = None,
) -> None:
    """Discover dependency updates and open an idempotent wrapper-chart PR."""
    mode = output_mod.resolve(output, ctx, allowed=_OUTPUTS, console=console)
    container = _container()
    workspace = container.workspace()
    result = run(
        UpgradeRequest(chart=resolve_chart_target(workspace, chart), dry_run=dry_run),
        workspace=workspace,
        runner=container.command_runner(),
        settings=container.settings,
        events=container.event_writer(),
    )
    output_mod.finish(result, mode=mode, render=_render_text)


def upgrade_finalize(
    ctx: typer.Context,
    path: Annotated[Path, typer.Option("--path", help="Repository-relative wrapper chart path.")],
    data_file: Annotated[
        Path | None,
        typer.Option(
            "--data-file",
            envvar=_CALLBACK_DATA_ENV,
            help="Renovate callback data file (normally supplied by the callback environment).",
        ),
    ] = None,
    output: OutputOption = None,
) -> None:
    """Finalize the Renovate callback (internal; invoked by trusted configuration)."""
    mode = output_mod.resolve(output, ctx, allowed=_OUTPUTS, console=console)
    if data_file is None:
        raise ChartManagerError(f"--data-file is required (or set {_CALLBACK_DATA_ENV})")
    container = _container()
    workspace = container.workspace()
    update_data = load_update_data(data_file)
    # The loader follows symlinks, so check the same repository-relative path first.
    chart_dir = workspace.root / path
    reject_symlinks(chart_dir, workspace.root)
    result = finalize.run(
        FinalizeRequest(chart=chart_target(workspace.root, chart_dir), update_data=update_data),
        workspace=workspace,
        runner=container.command_runner(),
        settings=container.settings,
    )
    output_mod.finish(result, mode=mode, render=_render_text)


def _render_text(result: UpgradeResult | FinalizeResult) -> None:
    """Print each field of the result's document on its own line, `-` for an absent value."""
    for key, value in to_document(result).items():
        if isinstance(value, list):
            typer.echo(f"{key}:")
            for item in value or ["none"]:
                typer.echo(f"- {item}")
        else:
            typer.echo(f"{key}: {'-' if value is None else value}")


__all__ = [
    "upgrade",
    "upgrade_finalize",
]
