"""`chart-manager schemas sync` — hydrate the locked local schema store."""

from __future__ import annotations

from typing import Annotated

import typer

from chart_manager.cli._container import container as _container
from chart_manager.cli.streams import console
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
            help="Resolve tracking refs, cache repositories, and update the lock.",
        ),
    ] = False,
) -> None:
    """Cache complete upstream schema repositories at the committed pins."""
    result = _make_service().sync(update=update)
    action = "published" if result.generation_published else "ready"
    console.print(
        f"schema generation {result.lock.generation} {action} at {result.generation_path}"
    )


__all__ = ["register"]
