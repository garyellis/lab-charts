"""`chart list` and `chart show`: pure reads of the chart catalog.

Both hand a wire document from `commands/catalog/wire.py` to `output.emit` and build
their own table beside it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from chart_manager.cli import output as output_mod
from chart_manager.cli._container import container as _container
from chart_manager.cli.streams import console
from chart_manager.commands.catalog import run as catalog
from chart_manager.commands.catalog.wire import catalog_to_dict, lifecycle_to_dict
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for

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
    entries = catalog.list_charts(_container().workspace())
    output_mod.emit(catalog_to_dict(entries), mode=mode, table=_catalog_table(entries))
    # A chart whose lifecycle document does not load is reported *in* the
    # projection (as `error`, in every format) and again as the exit code, so
    # neither a reader nor a pipeline has to learn the other's channel. What
    # failed is the *authoring* of a `Chart.yaml` or `chart-lifecycle.yaml`,
    # which is a spec error -- exit 3, not the generic 1 this used to
    # return.
    if any(entry.error is not None for entry in entries):
        raise typer.Exit(code=exit_code_for(Outcome.SPEC))


def _catalog_table(entries: Sequence[catalog.ChartCatalogEntry]) -> Table:
    """Render the chart catalog as the terminal projection."""
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
    for entry in entries:
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
    return table


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
    document = lifecycle_to_dict(catalog.show_chart(_container().workspace(), chart))
    output_mod.emit(
        document,
        mode=mode,
        table=output_mod.document_table(document, title=f"{chart} lifecycle"),
    )
