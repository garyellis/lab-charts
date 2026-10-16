"""`chart list` and `chart show`: pure reads of the chart catalog."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from chart_manager.cli import output as output_mod
from chart_manager.cli._container import container as _container
from chart_manager.cli.streams import console
from chart_manager.commands.catalog import run as catalog
from chart_manager.plumbing.documents import to_document

#: `chart list` and `chart show` speak the core projections minus `md`:
#: neither has a markdown form, and advertising one the resolver cannot
#: produce is the lie `cli/output.py` exists to prevent.
_CHART_CATALOG_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML)

ChartCatalogOutputOption = Annotated[
    str | None,
    output_mod.output_option(*_CHART_CATALOG_OUTPUTS),
]


def list_charts(
    ctx: typer.Context,
    output: ChartCatalogOutputOption = None,
) -> None:
    """List Helm charts and their lifecycle capability status.

    `-o` defaults to `auto`: the table on a terminal, JSON in a pipe or in
    CI. The table was this command's only output for its whole life, which
    made `chart list | grep` a habit and the chart inventory unreadable to
    anything else; the JSON payload is the chart catalog document, so a
    second surface answers this question with the same bytes.
    """
    mode = output_mod.resolve(output, ctx, allowed=_CHART_CATALOG_OUTPUTS, console=console)
    # A chart that fails to load is reported both as its `error` and as the
    # exit code, so neither a reader nor a pipeline needs the other's channel.
    output_mod.finish(
        catalog.list_charts(_container().workspace()), mode=mode, render=_print_catalog
    )


def _print_catalog(chart_catalog: catalog.ChartCatalog) -> None:
    """Print the chart catalog as a table."""
    table = Table(
        "Chart",
        "Type",
        "Version",
        "Dependencies",
        "Lifecycle",
        "Manifest validation",
        "Chart tests",
        "Profiles",
    )
    for entry in chart_catalog.charts:
        lifecycle_status = (
            f"[red]invalid: {escape(entry.error or '')}[/red]"
            if entry.error is not None
            else entry.lifecycle_status
        )
        table.add_row(
            entry.name,
            entry.chart_type,
            entry.version,
            ", ".join(entry.dependencies),
            lifecycle_status,
            entry.validation.value,
            entry.chart_test.value,
            ", ".join(entry.profiles),
        )
    console.print(table)


def show_lifecycle(
    ctx: typer.Context,
    chart: str,
    output: ChartCatalogOutputOption = None,
) -> None:
    """Print one chart's normalized ChartLifecycle intent.

    `-o json`/`-o yaml` emit the authored envelope after normalization, so
    the output can be diffed against -- or pasted back into --
    `chart-lifecycle.yaml`. `-o table` flattens that same document onto
    dotted field paths for reading at a terminal, which is what `auto`
    selects there.
    """
    mode = output_mod.resolve(output, ctx, allowed=_CHART_CATALOG_OUTPUTS, console=console)
    document = to_document(catalog.show_chart(_container().workspace(), chart))
    output_mod.emit(
        document,
        mode=mode,
        table=output_mod.document_table(document, title=f"{chart} lifecycle"),
    )
