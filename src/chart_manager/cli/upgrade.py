"""CLI surface for Renovate-driven wrapper-chart upgrades.

The service owns discovery, preflight, isolated worktree mutation, and PR
idempotency. This module owns only Typer's flag shape and stable rendering.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Protocol

import typer

from chart_manager.composition import Container
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.upgrader import (
    FinalizeRequest,
    UpgradeRequest,
    load_update_data,
)

_FORMATS = ("text", "json")
_CALLBACK_DATA_ENV = "RENOVATE_POST_UPGRADE_COMMAND_DATA_FILE"


class _WireResult(Protocol):
    """Marker protocol for an upgrade result."""


class _UpgradeService(Protocol):
    def upgrade(self, request: UpgradeRequest) -> _WireResult:
        """Plan and execute one chart upgrade."""
        ...


class _FinalizeService(Protocol):
    def finalize(self, request: FinalizeRequest) -> _WireResult:
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
    path: Annotated[Path, typer.Option("--path", help="Repository-relative wrapper chart path.")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Discover and plan without pushing or opening a PR."),
    ] = False,
    format: FormatOption = "text",
) -> None:
    """Discover dependency updates and open an idempotent wrapper-chart PR."""
    root = Path(".").resolve()
    result = _make_upgrade_service(root).upgrade(
        UpgradeRequest(root=root, chart_path=path, dry_run=dry_run)
    )
    _emit(result, format=format, chart_path=path)


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
    _emit(result, format=format, chart_path=path)


def _emit(result: _WireResult, *, format: str, chart_path: Path | None = None) -> None:
    """Write stable machine or human output from the service wire projection."""
    payload = _wire_payload(result, chart_path=chart_path)
    if format == "json":
        typer.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return
    typer.echo(_render_text(payload))


def _wire_payload(result: _WireResult, *, chart_path: Path | None) -> dict[str, Any]:
    """Project either public result dataclass onto the common CLI contract."""
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        raw = dict(to_dict())
    elif is_dataclass(result) and not isinstance(result, type):
        raw = asdict(result)
    else:
        raise TypeError("upgrade result must be a dataclass or define to_dict()")

    current = _first(raw, "current_wrapper_version", "current_version", "previous_version")
    proposed = _first(raw, "proposed_wrapper_version", "proposed_version", "version")
    changed = raw.get("changed")
    outcome = raw.get("outcome")
    if outcome is None and isinstance(changed, bool):
        outcome = "updated" if changed else "unchanged"
    path = _first(raw, "path", "chart_path")
    if path is None:
        path = chart_path
    pull_request = raw.get("pull_request")
    if pull_request is None and (
        raw.get("pr_url") is not None or raw.get("pr_number") is not None
    ):
        pull_request = {
            "url": raw.get("pr_url"),
            "number": raw.get("pr_number"),
        }
    payload = {
        "schema_version": 1,
        "repository": raw.get("repository"),
        "base": raw.get("base"),
        "chart": raw.get("chart"),
        "path": path,
        "current_wrapper_version": current,
        "proposed_wrapper_version": proposed,
        "branch": raw.get("branch"),
        "outcome": outcome,
        "pull_request": pull_request,
        "diagnostics": raw.get("diagnostics", ()),
    }
    return _json_value(payload)


def _json_value(value: Any) -> Any:
    """Normalize paths, enums, mappings, and tuples for deterministic JSON."""
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


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
        pr = str(pull_request or payload.get("pr_url") or "-")

    diagnostics = payload.get("diagnostics")
    lines = [
        f"repository: {_shown(payload.get('repository'))}",
        f"base: {_shown(payload.get('base'))}",
        f"chart: {_shown(payload.get('chart'))}",
        f"path: {_shown(payload.get('path'))}",
        f"current wrapper version: {_shown(_first(payload, 'current_wrapper_version', 'current_version'))}",
        f"proposed wrapper version: {_shown(_first(payload, 'proposed_wrapper_version', 'proposed_version'))}",
        f"branch: {_shown(payload.get('branch'))}",
        f"outcome: {_shown(payload.get('outcome'))}",
        f"pull request: {pr}",
        "diagnostics:",
    ]
    if isinstance(diagnostics, (list, tuple)) and diagnostics:
        lines.extend(f"- {_diagnostic(item)}" for item in diagnostics)
    elif diagnostics:
        lines.append(f"- {_diagnostic(diagnostics)}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _shown(value: Any) -> str:
    return "-" if value is None or value == "" else str(value)


def _diagnostic(value: Any) -> str:
    if isinstance(value, Mapping):
        message = value.get("message")
        if message is not None:
            return str(message)
        return json.dumps(dict(value), separators=(",", ":"), sort_keys=True)
    return str(value)


def register(app: typer.Typer) -> None:
    """Attach the public command and Renovate-only hidden callback."""
    app.command("upgrade")(upgrade)
    app.command("upgrade-finalize", hidden=True)(upgrade_finalize)


__all__ = [
    "_make_finalize_service",
    "_make_upgrade_service",
    "register",
    "upgrade",
    "upgrade_finalize",
]
