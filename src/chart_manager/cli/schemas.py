"""`chart-manager schemas sync` — hydrate the locked local schema store."""

from __future__ import annotations

from typing import Annotated

import typer

from chart_manager.cli._container import container as _container
from chart_manager.cli.streams import console, narration
from chart_manager.services.kubeconform_schemas.app import (
    RepositoryKubeconformSchemaService,
)


def register(app: typer.Typer) -> None:
    app.command("sync")(sync)


def _make_service() -> RepositoryKubeconformSchemaService:
    container = _container()
    return container.kubeconform_schema_service(container.workspace().root)


def sync(
    ctx: typer.Context,
    update: Annotated[
        bool,
        typer.Option(
            "--update",
            help="Resolve tracking refs, build a complete generation, and update the lock.",
        ),
    ] = False,
    offline: Annotated[
        bool | None,
        typer.Option(
            "--offline/--online",
            help="Forbid network access and require the locked generation to be present.",
        ),
    ] = None,
    workers: Annotated[
        int,
        typer.Option("--workers", min=0, help="Render workers; 0 uses the normal default."),
    ] = 0,
) -> None:
    """Eagerly render, inventory, verify, and publish all required schemas."""
    configured_offline = bool(getattr(ctx.obj, "offline", False))
    result = _make_service().sync(
        update=update,
        offline=configured_offline if offline is None else offline,
        workers=workers,
    )
    action = "published" if result.sync.generation_published else "ready"
    console.print(
        f"schema generation {result.sync.lock.generation} {action} at "
        f"{result.sync.generation_path}"
    )
    narration.print(
        f"{result.rows} rows; {result.required} requirements; "
        f"{result.generated} generated; {result.local} local"
    )


__all__ = ["register"]
