"""CLI surface for Renovate-driven wrapper-chart upgrades.

The service owns discovery, preflight, isolated worktree mutation, and PR
idempotency. `services/upgrader/wire.py` owns the versioned machine-readable
contract. This module owns only Typer's flag shape, the encoder settings, and
the human-readable rendering.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Protocol

import typer

from chart_manager.composition import Container, Settings
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.local_resources import resolve_chart_target
from chart_manager.services.upgrader import (
    FinalizeRequest,
    FinalizeResult,
    UpgradeRequest,
    UpgradeResult,
    load_update_data,
)
from chart_manager.services.upgrader.wire import finalize_to_dict, upgrade_to_dict

_FORMATS = ("text", "json")
_CALLBACK_DATA_ENV = "RENOVATE_POST_UPGRADE_COMMAND_DATA_FILE"


class _UpgradeService(Protocol):
    def upgrade(self, request: UpgradeRequest) -> UpgradeResult:
        """Plan and execute one chart upgrade."""
        ...


class _FinalizeService(Protocol):
    def finalize(self, request: FinalizeRequest) -> FinalizeResult:
        """Finalize a Renovate callback."""
        ...


def _container() -> Container:
    """Build one composition root (module-level so tests can replace it)."""
    return Container()


def _make_upgrade_service(root: Path) -> _UpgradeService:
    """Build the public upgrade service through the composition root."""
    return _container().upgrade_service(root)


def _make_finalize_service(root: Path) -> _FinalizeService:
    """Build the internal callback service through the composition root."""
    return _container().upgrade_finalizer(root)


def _format_choice(value: str) -> str:
    if value not in _FORMATS:
        raise typer.BadParameter(
            f"unknown format: {value} (allowed: {', '.join(_FORMATS)})",
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


def upgrade(
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
    format: FormatOption = "text",
) -> None:
    """Discover dependency updates and open an idempotent wrapper-chart PR."""
    root = Path(".").resolve()
    result = _make_upgrade_service(root).upgrade(
        UpgradeRequest(root=root, chart_path=_chart_path(chart, path, root=root), dry_run=dry_run)
    )
    _emit(upgrade_to_dict(result), format=format)


def _chart_path(chart: str | None, path: Path | None, *, root: Path) -> Path:
    """Resolve the one chart this invocation names, however it was spelled.

    `--path` is the frozen-in-muscle-memory spelling and stays verbatim: it
    is a repository-relative path and the service has always taken it as
    one. The CHART argument goes through `resolve_chart_target`, the same
    resolver `chart test` and `chart validate` use, so a bare chart name
    means the same thing in all three — and so this module contains no path
    heuristic of its own (design commitment 6).
    """
    if (chart is None) == (path is None):
        raise ChartManagerError("name exactly one chart, as the CHART argument or --path")
    if path is not None:
        return path
    assert chart is not None
    settings = Settings()
    target = resolve_chart_target(
        root,
        chart,
        charts_dir=settings.charts_dir,
        local_config=settings.local_config,
    )
    return target.path.relative_to(root)


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
    root = Path(".").resolve()
    update_data = load_update_data(data_file, repo_root=root)
    result = _make_finalize_service(root).finalize(
        FinalizeRequest(repo_root=root, chart_path=path, update_data=update_data)
    )
    _emit(finalize_to_dict(result, chart_path=path), format=format)


def _emit(payload: Mapping[str, Any], *, format: str) -> None:
    """Encode one wire payload as machine or human output."""
    if format == "json":
        typer.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return
    typer.echo(_render_text(payload))


def _render_text(payload: Mapping[str, Any]) -> str:
    """Render every contract field in a fixed order, including absent values."""
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, Mapping):
        pr_url = pull_request.get("url")
        pr_number = pull_request.get("number")
        if pr_url and pr_number is not None:
            pr = f"#{pr_number} {pr_url}"
        else:
            pr = str(pr_url or pr_number or "-")
    else:
        pr = "-"

    diagnostics = payload.get("diagnostics")
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
    if isinstance(diagnostics, (list, tuple)) and diagnostics:
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
    """Attach the Renovate-only hidden callback to the *root* Typer app.

    Separate from `register_upgrade` because these two go to different
    places and one of them may never move: `renovate-global.json` pins the
    literal string `chart-manager upgrade-finalize --path <dir>` in an
    allowlist regex, so this command is root-level and frozen. Registering
    both from one function is what would make relocating `upgrade` quietly
    relocate `upgrade-finalize` with it.
    """
    app.command("upgrade-finalize", hidden=True)(upgrade_finalize)


__all__ = [
    "_make_finalize_service",
    "_make_upgrade_service",
    "register_finalize",
    "register_upgrade",
    "upgrade",
    "upgrade_finalize",
]
