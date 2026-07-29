"""CLI surface for batch OCI publishing."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from chart_manager.composition import Container

console = Console()


def _container() -> Container:
    return Container()


def publish(
    charts: Annotated[list[str], typer.Argument(help="One or more chart directory names.")],
    repository: Annotated[
        str,
        typer.Option(
            "--repository",
            envvar="CHART_MANAGER_OCI_REPOSITORY",
            help="Destination OCI repository, for example oci://harbor.local/library.",
        ),
    ],
    root: Annotated[Path, typer.Option("--root", help="Repository root.")] = Path("."),
    version_suffix: Annotated[
        str | None,
        typer.Option(
            "--version-suffix",
            help="SemVer prerelease suffix appended to every chart's base version.",
        ),
    ] = None,
    version: Annotated[
        str | None,
        typer.Option("--version", help="Exact package version; valid for one chart only."),
    ] = None,
    ca_file: Annotated[
        Path | None,
        typer.Option(
            "--ca-file",
            envvar="CHART_MANAGER_OCI_CA_FILE",
            help="CA bundle used to verify the OCI registry.",
        ),
    ] = None,
) -> None:
    """Package all requested charts before pushing any of them."""
    result = _container().publish_service(root).publish(
        charts,
        repository=repository,
        version_suffix=version_suffix,
        version=version,
        ca_file=ca_file,
    )
    for chart in result.charts:
        if chart.ok:
            digest = f" ({chart.digest})" if chart.digest else ""
            console.print(
                f"published [bold]{escape(chart.chart)}[/bold] "
                f"{escape(chart.reference or '')}{escape(digest)}"
            )
        else:
            console.print(
                f"[red]failed[/red] [bold]{escape(chart.chart)}[/bold]: "
                f"{escape(chart.error or 'unknown push failure')}"
            )
    if not result.ok:
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    """Attach the top-level publish command."""
    app.command("publish")(publish)


__all__ = ["publish", "register"]
