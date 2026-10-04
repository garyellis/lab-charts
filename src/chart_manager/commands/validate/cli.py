"""Typer commands owned by the validate package: `schemas sync`."""

from __future__ import annotations

from typing import Annotated

import typer

from chart_manager.cli._container import container as _container
from chart_manager.cli.streams import console
from chart_manager.commands.validate.schemas.app import sync as sync_schemas
from chart_manager.commands.validate.schemas.store import KubeconformSchemaStore
from chart_manager.integrations.kubeconform import GitHubKubeconformSchemaSource


def register_schemas(app: typer.Typer) -> None:
    """Attach `schemas sync` to the `schemas` Typer group."""
    app.command("sync")(sync)


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
    container = _container()
    timeout = container.settings.command_timeout
    result = sync_schemas(
        container.workspace(),
        store=KubeconformSchemaStore(),
        source=GitHubKubeconformSchemaSource(
            timeout=timeout if timeout is not None and timeout > 0 else 15.0
        ),
        update=update,
    )
    action = "published" if result.generation_published else "ready"
    console.print(
        f"schema generation {result.lock.generation} {action} at {result.generation_path}"
    )
