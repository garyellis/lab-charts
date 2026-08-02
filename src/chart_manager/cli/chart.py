"""`chart list`, `chart show`, `chart test` -- reading and exercising charts.

The rest of the `chart` group lives elsewhere and always has:
`cli/validate.py` owns `validate` and the `cache` subgroup, `cli/publish.py`
owns `publish`, `cli/upgrade.py` owns `upgrade`. What is left here are the
three commands that had no module of their own and so stayed in `main.py`,
which is how `main.py` came to be a thousand lines longer than every other
file under `cli/`.

`list` and `show` are pure reads: both hand a wire document from
`services/chart_catalog_wire.py` to `output.emit` and build their own table
projection beside it. `test` is the only one that touches a cluster, and it
owns none of that -- `EphemeralTestCluster` compiles and runs the plan, and
this module chooses between printing the plan and running it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from chart_manager.cli import output as output_mod
from chart_manager.cli._container import container as _container
from chart_manager.cli._container import resolve_chart
from chart_manager.cli._options import ClusterNameOption, RootOption
from chart_manager.cli.streams import console, narration
from chart_manager.cli.streams import print_progress as _print_progress
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
from chart_manager.services.chart_catalog import ChartCatalogEntry
from chart_manager.services.chart_catalog_wire import catalog_to_dict, lifecycle_to_dict
from chart_manager.services.clusters.ephemeral import (
    DEFAULT_CLUSTER_NAME,
    DEFAULT_PROFILE,
    EphemeralTestRequest,
)
from chart_manager.services.lifecycle.models import LifecyclePlan
from chart_manager.services.lifecycle.wire import plan_to_dict

ProfileOption = Annotated[str, typer.Option("--profile", help="Cluster-test profile.")]
NamespaceOverrideOption = Annotated[
    str | None,
    typer.Option(
        "--namespace",
        help="Override the namespace declared by the selected ChartLifecycle profile.",
    ),
]

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
_DRY_RUN_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML)

DryRunOutputOption = Annotated[
    str | None,
    output_mod.output_option(*_DRY_RUN_OUTPUTS, extra_help=" Requires --dry-run."),
]


def register(app: typer.Typer) -> None:
    """Attach the read-and-exercise commands to the `chart` Typer group.

    Registration order is `--help` order, and `main.py` calls this after the
    modules that own `validate`, `publish` and `upgrade`, which is where
    these three sat when they were decorated inline.
    """
    app.command("list")(list_charts)
    app.command("test")(chart_test)
    app.command("show")(show_lifecycle)


def list_charts(
    ctx: typer.Context,
    output: ChartCatalogOutputOption = None,
    root: RootOption = Path("."),
) -> None:
    """List Helm charts and their lifecycle capability status.

    `-o` defaults to `auto`: the table on a terminal, JSON in a pipe or in
    CI. The table was this command's only output for its whole life, which
    made `chart list | grep` a habit and the chart inventory unreadable to
    anything else; the JSON payload is the versioned document in
    `services/chart_catalog_wire.py`, so a second surface answers this
    question with the same bytes.
    """
    mode = output_mod.resolve(output, ctx, allowed=_CHART_CATALOG_OUTPUTS, console=console)
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
        "Cluster tests",
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
            entry.cluster_test.value,
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


def _run_chart_test(
    chart: str,
    *,
    ctx: typer.Context,
    root: Path,
    profile: str,
    namespace: str | None,
    cluster_name: str,
    dependent_tests: bool,
    no_ensure_cluster: bool,
    lint: bool,
    dry_run: bool,
    output: str | None,
) -> None:
    """Run one chart test through the canonical charts command."""
    root = root.resolve()
    target = resolve_chart(root, chart)
    charts_dir = target.path.parent.relative_to(root)
    service = _container().ephemeral_test_cluster_service(
        root,
        progress=_print_progress,
        charts_dir=charts_dir,
    )
    request = EphemeralTestRequest(
        chart=target.name,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        ensure_cluster=not no_ensure_cluster,
        include_dependent_tests=dependent_tests,
        lint=lint,
    )
    if dry_run:
        _render_test_plan(service.plan(request), ctx=ctx, output=output)
        return
    service.run(request)


def _render_test_plan(plan: LifecyclePlan, *, ctx: typer.Context, output: str | None) -> None:
    """Print the compiled cluster-test plan; say on stderr what did not happen.

    The plan is what the caller asked for, so it is the projection and goes
    to stdout. That it was *only* a plan is narration, and stays off the
    stream a `-o json | jq` consumer reads.
    """
    mode = output_mod.resolve(output, ctx, allowed=_DRY_RUN_OUTPUTS, console=console)
    table = Table("Step", "Action", "Chart", "Profile", "Namespace", "Release")
    for step, action in enumerate(plan.actions, start=1):
        table.add_row(
            str(step),
            action.kind.value,
            action.target.chart,
            action.target.profile or "",
            action.target.namespace or "",
            action.target.release or "",
        )
    output_mod.emit(plan_to_dict(plan), mode=mode, table=table)
    for warning in plan.warnings:
        narration.print(f"[yellow]warn:[/yellow] {escape(warning)}")
    narration.print(
        "[yellow]dry run[/yellow]: no cluster was created, nothing was installed or tested"
    )


def chart_test(
    ctx: typer.Context,
    chart_argument: Annotated[
        str | None,
        typer.Argument(metavar="[CHART]", help="Chart name or chart directory."),
    ] = None,
    chart: Annotated[
        str | None,
        typer.Option(
            "--chart",
            help="Chart name or chart directory. Retained alongside the CHART argument.",
        ),
    ] = None,
    root: RootOption = Path("."),
    profile: ProfileOption = DEFAULT_PROFILE,
    namespace: NamespaceOverrideOption = None,
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    dependent_tests: Annotated[
        bool,
        typer.Option(
            "--dependent-tests",
            help="Run cluster tests affected by this chart.",
        ),
    ] = False,
    no_ensure_cluster: Annotated[
        bool,
        typer.Option("--no-ensure-cluster", help="Do not create the test cluster if missing."),
    ] = False,
    lint: Annotated[bool, typer.Option("--lint", help="Run helm lint before install.")] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the cluster-test plan and exit; create no cluster, install nothing.",
        ),
    ] = False,
    output: DryRunOutputOption = None,
) -> None:
    """Install and exercise one chart on an ephemeral local Kubernetes cluster.

    The chart is named positionally; `--chart` is the older spelling and is
    kept permanently, because `.github/workflows/ci.yaml` uses it and the
    flag costs nothing to keep once the positional exists.

    `--dry-run` prints the compiled lifecycle plan -- every namespace,
    install and helm test the run would perform, in order -- and exits 0
    having touched nothing. It is the same plan object the real run
    executes, so it cannot describe work that would not happen.

    `-o` selects the form that plan is printed in. It is only meaningful
    with `--dry-run` -- a real run's report is progress narration, not a
    document -- so naming it without `--dry-run` is a usage error rather
    than a flag that is quietly ignored.
    """
    if (chart_argument is None) == (chart is None):
        raise ChartManagerError("name exactly one chart, as the CHART argument or --chart")
    output_mod.require_dry_run(output, dry_run=dry_run)
    selected = chart_argument if chart_argument is not None else chart
    assert selected is not None
    _run_chart_test(
        selected,
        ctx=ctx,
        output=output,
        root=root,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        dependent_tests=dependent_tests,
        no_ensure_cluster=no_ensure_cluster,
        lint=lint,
        dry_run=dry_run,
    )


def show_lifecycle(
    ctx: typer.Context,
    chart: str,
    output: ChartCatalogOutputOption = None,
    root: RootOption = Path("."),
) -> None:
    """Print one chart's normalized ChartLifecycle intent.

    `-o json`/`-o yaml` emit the authored envelope after normalization, so
    the output can be diffed against -- or pasted back into --
    `chart-lifecycle.yaml`. `-o table` flattens that same document onto
    dotted field paths for reading at a terminal, which is what `auto`
    selects there.
    """
    mode = output_mod.resolve(output, ctx, allowed=_CHART_CATALOG_OUTPUTS, console=console)
    document = lifecycle_to_dict(_container().chart_catalog_service(root).get_lifecycle(chart))
    output_mod.emit(
        document,
        mode=mode,
        table=_document_table(document, title=f"{chart} lifecycle"),
    )


__all__ = ["register"]
