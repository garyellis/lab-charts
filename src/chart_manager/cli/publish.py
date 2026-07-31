"""CLI surface for batch OCI publishing."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from chart_manager.cli.streams import narration_console
from chart_manager.composition import Container
from chart_manager.services.publish import PublishKind

#: `publish` has no `--output` projection: every line it prints is a
#: per-chart mutation status, so all of it narrates on stderr. When
#: `--dry-run`/`-o json` lands this module gains a `data_console()` for the
#: payload and these lines stay exactly where they are.
narration = narration_console()


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
    publish_kind: Annotated[
        PublishKind | None,
        typer.Option(
            "--publish-kind",
            help="Artifact lifecycle meaning; inferred as preview with --version-suffix, release otherwise.",
        ),
    ] = None,
    build_correlation_id: Annotated[
        str | None,
        typer.Option(help="Charts-repo build identifier, conventionally owner/repo#PR."),
    ] = None,
    pr_url: Annotated[str | None, typer.Option(help="Originating pull-request URL.")] = None,
    git_sha: Annotated[str | None, typer.Option(help="Published source commit SHA.")] = None,
    operation_id: Annotated[
        str | None,
        typer.Option(help="CI run or batch identifier included in event detail."),
    ] = None,
    strict_events: Annotated[
        bool,
        typer.Option(help="Exit nonzero when a publication event cannot be persisted."),
    ] = False,
) -> None:
    """Package all requested charts before pushing any of them."""
    result = _container().publish_service(root).publish(
        charts,
        repository=repository,
        version_suffix=version_suffix,
        version=version,
        ca_file=ca_file,
        publish_kind=publish_kind,
        build_correlation_id=build_correlation_id,
        pr_url=pr_url,
        git_sha=git_sha,
        operation_id=operation_id,
    )
    for chart in result.charts:
        if chart.ok:
            digest = f" ({chart.digest})" if chart.digest else ""
            narration.print(
                f"published [bold]{escape(chart.chart)}[/bold] "
                f"{escape(chart.reference or '')}{escape(digest)}"
            )
        else:
            narration.print(
                f"[red]failed[/red] [bold]{escape(chart.chart)}[/bold]: "
                f"{escape(chart.error or 'unknown push failure')}"
            )
    for failure in result.telemetry_failures:
        narration.print(
            f"[yellow]event failed[/yellow] [bold]{escape(failure.chart)}[/bold] "
            f"{escape(failure.version)}: {escape(failure.error)}"
        )
    if not result.ok or (strict_events and not result.telemetry_ok):
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    """Attach the top-level publish command."""
    app.command("publish")(publish)


__all__ = ["publish", "register"]
