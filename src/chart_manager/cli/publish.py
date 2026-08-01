"""CLI surface for batch OCI publishing."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from chart_manager.cli.streams import data_console, narration_console
from chart_manager.composition import Container
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
from chart_manager.services.publish import PublishKind, PublishResult

#: Every line a real publish prints is a per-chart mutation status -- a report
#: of something that already happened -- so all of it narrates on stderr.
narration = narration_console()

#: A `--dry-run` plan is the opposite: it *is* the thing the caller asked for,
#: so it is the selected projection and goes to stdout. `publish` has no
#: `--output` flag yet, so the selected projection is text; when the global
#: `-o` lands (P1.4) the json form renders from `PublishResult` on this same
#: console and the stream assignment does not move.
data = data_console()


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
            "--kind",
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
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Package the charts and print the push plan; push nothing, emit no event.",
        ),
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
        dry_run=dry_run,
    )
    if dry_run:
        _render_plan(result)
        return
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
        raise typer.Exit(code=exit_code_for(Outcome.FAILED))


def _render_plan(result: PublishResult) -> None:
    """Print the dry-run plan to stdout and say on stderr what did not happen.

    Exits 0 by falling off the end: a plan that was produced is a success,
    and there is no push outcome to fail on.
    """
    kind = result.publish_kind.value if result.publish_kind is not None else "unknown"
    for chart in result.charts:
        data.print(
            f"would publish [bold]{escape(chart.chart)}[/bold] "
            f"{escape(chart.version)} -> {escape(chart.reference or '')} "
            f"({escape(kind)})"
        )
    narration.print(
        f"[yellow]dry run[/yellow]: packaged {len(result.charts)} chart(s); "
        "pushed nothing and emitted no lifecycle event"
    )


def register(app: typer.Typer) -> None:
    """Attach the top-level publish command."""
    app.command("publish")(publish)


__all__ = ["publish", "register"]
