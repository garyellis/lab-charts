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
    update: Annotated[
        bool,
        typer.Option(
            "--update",
            help="Resolve tracking refs, build a complete generation, and update the lock.",
        ),
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="Rebuild derived requirements using the commits already pinned in the lock.",
        ),
    ] = False,
    workers: Annotated[
        int,
        typer.Option("--workers", min=0, help="Render workers; 0 uses the normal default."),
    ] = 0,
) -> None:
    """Eagerly render, inventory, verify, and publish all required schemas."""
    if update and refresh:
        raise typer.BadParameter("--update and --refresh are mutually exclusive")
    result = _make_service().sync(
        update=update,
        refresh=refresh,
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
