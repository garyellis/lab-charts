"""`chart list` and `chart show`: pure reads of the chart catalog.

Both hand a wire document from `services/chart_catalog_wire.py` to `output.emit` and build
their own table beside it. `chart test` and `chart teardown` live in `commands/test` and
register between them, which is their `--help` order.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from chart_manager.cli import output as output_mod
from chart_manager.cli._container import container as _container
from chart_manager.cli._container import repository_root
from chart_manager.cli.streams import console
from chart_manager.commands.test import cli as test_cli
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
from chart_manager.services.chart_catalog import ChartCatalogEntry
from chart_manager.services.chart_catalog_wire import catalog_to_dict, lifecycle_to_dict

#: `chart list` and `chart show` speak the core projections minus `md`:
#: neither has a markdown form, and advertising one the resolver cannot
#: produce is the lie `cli/output.py` exists to prevent.
_CHART_CATALOG_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML)

ChartCatalogOutputOption = Annotated[
    str | None,
    output_mod.output_option(*_CHART_CATALOG_OUTPUTS),
]

#: The vocabulary a `--dry-run` plan is printed in. Same three projections;
#: named separately because the document is a *plan*, not a catalog, and the
#: two have no reason to stay equal.
def register(app: typer.Typer) -> None:
    """Attach the read-and-exercise commands to the `chart` Typer group.

    Registration order is `--help` order, and `main.py` calls this after the
    modules that own `validate`, `publish` and `upgrade`, which is where
    these three sat when they were decorated inline.
    """
    app.command("list")(list_charts)
    test_cli.register(app)
    app.command("show")(show_lifecycle)


def list_charts(
    ctx: typer.Context,
    output: ChartCatalogOutputOption = None,
) -> None:
    """List Helm charts and their lifecycle capability status.

    `-o` defaults to `auto`: the table on a terminal, JSON in a pipe or in
    CI. The table was this command's only output for its whole life, which
    made `chart list | grep` a habit and the chart inventory unreadable to
    anything else; the JSON payload is the document in
    `services/chart_catalog_wire.py`, so a second surface answers this
    question with the same bytes.
    """
    mode = output_mod.resolve(output, ctx, allowed=_CHART_CATALOG_OUTPUTS, console=console)
    root = repository_root()
    entries = _container().chart_catalog_service(root).list_entries()
    output_mod.emit(catalog_to_dict(entries), mode=mode, table=_catalog_table(entries))
    # A chart whose lifecycle document does not load is reported *in* the
    # projection (as `error`, in every format) and again as the exit code, so
    # neither a reader nor a pipeline has to learn the other's channel. What
    # failed is the *authoring* of a `Chart.yaml` or `chart-lifecycle.yaml`,
    # which is 6.1's spec error -- exit 3, not the generic 1 this used to
    # return.
    if any(entry.error is not None for entry in entries):
        raise typer.Exit(code=exit_code_for(Outcome.SPEC))


def _catalog_table(entries: Sequence[ChartCatalogEntry]) -> Table:
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


def _document_table(document: dict[str, Any], *, title: str) -> Table:
    """Render a wire document as a Field/Value table over dotted paths.

    A flattening of the same document `-o json` emits rather than a
    hand-written layout, because `chart show`'s subject is the authored
    ChartLifecycle envelope and that schema grows a section at a time. A
    bespoke renderer would need editing every time the spec does, and until
    someone did it would silently omit whatever it had not been taught --
    which is the one thing a command called `show` must never do.
    """
    table = Table("Field", "Value", title=title)
    for field, value in _flatten(document):
        table.add_row(escape(field), escape(value))
    return table


def _flatten(value: Any, prefix: str = "") -> Iterator[tuple[str, str]]:
    """Walk a JSON-shaped document into (dotted path, rendered leaf) rows.

    A list of scalars stays on one row (`values: a.yaml, b.yaml`) because
    that is how it reads in the file it came from; a list of objects is
    indexed, because its members have structure worth addressing.
    """
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from _flatten(item, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, list) and value:
        if any(isinstance(item, dict | list) for item in value):
            for index, item in enumerate(value):
                yield from _flatten(item, f"{prefix}[{index}]")
        else:
            yield prefix, ", ".join(_leaf(item) for item in value)
    else:
        yield prefix, _leaf(value)


def _leaf(value: Any) -> str:
    """Render one leaf as JSON spells it, minus the quotes around strings.

    So a reader sees `true`/`null`/`{}` -- the tokens they would type back
    into the document -- and not Python's `True`/`None`/`{}`.
    """
    return value if isinstance(value, str) else json.dumps(value)


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
    root = repository_root()
    document = lifecycle_to_dict(_container().chart_catalog_service(root).get_lifecycle(chart))
    output_mod.emit(
        document,
        mode=mode,
        table=_document_table(document, title=f"{chart} lifecycle"),
    )


__all__ = ["register"]
