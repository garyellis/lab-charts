"""`chart upgrade` and the hidden `upgrade-finalize`: flags, output encoding and rendering.

`commands/upgrade/wire.py` owns the machine-readable contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from chart_manager.cli import output as output_mod
from chart_manager.cli._container import container as _container
from chart_manager.commands.upgrade import finalize
from chart_manager.commands.upgrade.finalize import load_update_data
from chart_manager.commands.upgrade.models import FinalizeRequest, UpgradeRequest
from chart_manager.commands.upgrade.run import run
from chart_manager.commands.upgrade.wire import finalize_to_dict, upgrade_to_dict
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.shared.charts.chart import resolve_chart_target
from chart_manager.shared.workspace import RepositoryWorkspace

#: `upgrade-finalize` keeps `--format text|json`: `renovate-global.json`'s allowlist pins the
#: command Renovate runs, so its surface does not follow `chart upgrade`'s `-o`.
_FINALIZE_FORMATS = ("text", "json")
_CALLBACK_DATA_ENV = "RENOVATE_POST_UPGRADE_COMMAND_DATA_FILE"

_UPGRADE_OUTPUTS = (output_mod.TABLE, output_mod.JSON)


def _format_choice(value: str) -> str:
    if value not in _FINALIZE_FORMATS:
        raise typer.BadParameter(
            f"unknown format: {value} (allowed: {', '.join(_FINALIZE_FORMATS)})",
            param_hint="--format",
        )
    return value


FormatOption = Annotated[
    str,
    typer.Option(
        "--format",
        help="Output format: text (default) or json.",
        callback=_format_choice,
    ),
]

#: The public `chart upgrade`'s output flag.
OutputOption = Annotated[str | None, output_mod.output_option(*_UPGRADE_OUTPUTS)]


def upgrade(
    ctx: typer.Context,
    chart: Annotated[
        str | None,
        typer.Argument(metavar="[CHART]", help="Chart name or repository-relative chart path."),
    ] = None,
    path: Annotated[
        Path | None,
        typer.Option(
            "--path",
            help="Repository-relative wrapper chart path. Retained alias for the CHART argument.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Discover and plan without pushing or opening a PR."),
    ] = False,
    output: OutputOption = None,
) -> None:
    """Discover dependency updates and open an idempotent wrapper-chart PR."""
    mode = output_mod.resolve(output, ctx, allowed=_UPGRADE_OUTPUTS)
    container = _container()
    workspace = container.workspace()
    result = run(
        UpgradeRequest(
            root=workspace.root,
            chart_path=_chart_path(chart, path, workspace=workspace),
            dry_run=dry_run,
        ),
        workspace=workspace,
        runner=container.command_runner(),
        events=container.event_writer(),
    )
    _emit(upgrade_to_dict(result), as_json=mode == output_mod.JSON)


def _chart_path(chart: str | None, path: Path | None, *, workspace: RepositoryWorkspace) -> Path:
    """Resolve the one chart named by CHART (via `resolve_chart_target`) or by `--path` as given."""
    if path is not None and chart is None:
        return path
    if chart is not None and path is None:
        return resolve_chart_target(workspace, chart).path.relative_to(workspace.root)
    raise ChartManagerError("name exactly one chart, as the CHART argument or --path")


def upgrade_finalize(
    path: Annotated[Path, typer.Option("--path", help="Repository-relative wrapper chart path.")],
    data_file: Annotated[
        Path | None,
        typer.Option(
            "--data-file",
            envvar=_CALLBACK_DATA_ENV,
            help="Renovate callback data file (normally supplied by the callback environment).",
        ),
    ] = None,
    format: FormatOption = "text",
) -> None:
    """Finalize the Renovate callback (internal; invoked by trusted configuration)."""
    if data_file is None:
        raise ChartManagerError(f"--data-file is required (or set {_CALLBACK_DATA_ENV})")
    container = _container()
    workspace = container.workspace()
    update_data = load_update_data(data_file)
    result = finalize.run(
        FinalizeRequest(repo_root=workspace.root, chart_path=path, update_data=update_data),
        workspace=workspace,
        runner=container.command_runner(),
    )
    _emit(finalize_to_dict(result, chart_path=path), as_json=format == "json")


def _emit(payload: Mapping[str, Any], *, as_json: bool) -> None:
    """Encode one wire payload as JSON or as text."""
    if as_json:
        typer.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return
    typer.echo(_render_text(payload))


def _render_text(payload: Mapping[str, Any]) -> str:
    """Render every contract field in a fixed order, including absent values."""
    pull_request = payload["pull_request"]
    if pull_request is None:
        pr = "-"
    elif pull_request["url"] and pull_request["number"] is not None:
        pr = f"#{pull_request['number']} {pull_request['url']}"
    else:
        pr = str(pull_request["url"] or pull_request["number"] or "-")

    diagnostics = payload["diagnostics"]
    lines = [
        f"repository: {_shown(payload.get('repository'))}",
        f"base: {_shown(payload.get('base'))}",
        f"chart: {_shown(payload.get('chart'))}",
        f"path: {_shown(payload.get('path'))}",
        f"current wrapper version: {_shown(payload.get('current_wrapper_version'))}",
        f"proposed wrapper version: {_shown(payload.get('proposed_wrapper_version'))}",
        f"branch: {_shown(payload.get('branch'))}",
        f"outcome: {_shown(payload.get('outcome'))}",
        f"pull request: {pr}",
        "diagnostics:",
    ]
    if diagnostics:
        lines.extend(f"- {item}" for item in diagnostics)
    else:
        lines.append("- none")
    return "\n".join(lines)


def _shown(value: Any) -> str:
    return "-" if value is None or value == "" else str(value)


def register_upgrade(app: typer.Typer) -> None:
    """Attach the public upgrade command to the given Typer app (`chart`)."""
    app.command("upgrade")(upgrade)


def register_finalize(app: typer.Typer) -> None:
    """Attach the hidden Renovate callback to the root app, where the allowlist expects it."""
    app.command("upgrade-finalize", hidden=True)(upgrade_finalize)


__all__ = [
    "register_finalize",
    "register_upgrade",
    "upgrade",
    "upgrade_finalize",
]
