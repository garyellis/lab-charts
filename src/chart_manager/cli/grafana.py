"""`grafana dashboard export` and `grafana dashboard lint`.

Grafana-specific because both commands know things no generic chart command
should: that a dashboard is a deep JSON tree with a UID, that a committed one
has exactly one legal byte sequence, that the admin password lives in
`secret/<release>`. Keeping that under `grafana` rather than `chart` is what
leaves room for datasources and alert rules to arrive as siblings.

Both service imports are deferred to the command body. `dashboard_export`
pulls in the HTTP path and `dashboard_lint` the rule set, and neither is
wanted in the process for a `chart list`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from chart_manager.cli import output as output_mod
from chart_manager.cli._options import ClusterNameOption, RootOption
from chart_manager.cli._wiring import container as _container
from chart_manager.cli.streams import console, narration
from chart_manager.composition import Settings
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
from chart_manager.services.clusters.ephemeral import DEFAULT_CLUSTER_NAME, DEFAULT_NAMESPACE

NamespaceOption = Annotated[
    str,
    typer.Option("--namespace", help="Kubernetes namespace."),
]

#: Both dashboard commands speak the core vocabulary minus `md`: there is no
#: markdown projection of a dashboard document or of a lint report, and
#: `cli/output.py` offers `md` only where one exists.
_DASHBOARD_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML)

DashboardOutputOption = Annotated[
    str | None,
    output_mod.output_option(*_DASHBOARD_OUTPUTS),
]


def register(app: typer.Typer) -> None:
    """Attach the dashboard commands to the `grafana dashboard` Typer group."""
    app.command("export")(grafana_dashboard_export)
    app.command("lint")(grafana_dashboard_lint)


def grafana_dashboard_export(
    ctx: typer.Context,
    uid: Annotated[str, typer.Argument(help="Dashboard UID to export.")],
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    namespace: NamespaceOption = DEFAULT_NAMESPACE,
    release: Annotated[
        str,
        typer.Option(
            "--release", help="Grafana Helm release name (drives secret and service name)."
        ),
    ] = "grafana",
    to: Annotated[
        Path | None,
        typer.Option(
            "--to",
            help="Write the canonical dashboard JSON to this file. Missing parent directories are created. Default: the document goes to stdout.",
        ),
    ] = None,
    output: DashboardOutputOption = None,
) -> None:
    """Export a dashboard from a kind-deployed Grafana and normalize for git.

    Auth + connectivity are resolved from the cluster: the admin password is
    read from secret/<release>, then an ephemeral port-forward to svc/<release>
    carries the HTTP GET. No pre-existing port-forward required.

    `--to` is *where* the document goes; `-o` is what shape it takes. Until
    this command was renamed `-o` named the destination file, which is the one
    meaning it cannot keep now that every other command reads it as a format.
    The flip is deliberate and has no alias: a path handed to `-o` is a usage
    error that names `--to`, never a file written somewhere surprising.

    The file `--to` writes is always canonical JSON, whatever `-o` says --
    it is the git artifact, and a committed dashboard has exactly one legal
    byte sequence (`canonical_json`). `-o` therefore only decides what lands
    on stdout, and the document goes to exactly one place: with `--to` given,
    `json`/`yaml` print nothing and the file is the answer.

    `-o table` is a summary of what was exported rather than the document,
    because a dashboard is a deep tree with no table form and `auto` has to
    give a human at a terminal something readable. It prints in both cases,
    so an interactive `--to` export still says what it just wrote.
    """
    from chart_manager.services.grafana.dashboard_export import (
        ExportRequest,
        canonical_json,
        summarize_dashboard,
    )

    mode = output_mod.resolve(output, ctx, allowed=_DASHBOARD_OUTPUTS, console=console)
    # `fetch` rather than `export`: the normalized object is what the summary
    # and the yaml projection are derived from, and `canonical_json` is the
    # same function `export` would have applied.
    dashboard = (
        _container()
        .grafana_exporter()
        .fetch(
            ExportRequest(
                uid=uid,
                cluster_name=cluster_name,
                namespace=namespace,
                release=release,
            )
        )
    )
    if to is not None:
        try:
            to.parent.mkdir(parents=True, exist_ok=True)
            to.write_text(canonical_json(dashboard))
        except OSError as exc:
            # Same shape as `plan`'s `_changed_paths`: an OSError from a path
            # the caller typed into a flag is a usage error naming that flag,
            # not a traceback. A directory handed to `--to` was the traceback
            # case -- the write-side twin of the `--path DIR` lint defect.
            raise typer.BadParameter(
                f"cannot write dashboard to {to}: {exc}",
                param_hint="--to",
            ) from exc
        narration.print(f"[green]wrote[/green] {to}")

    if mode == output_mod.TABLE:
        summary = summarize_dashboard(dashboard)
        table = Table("UID", "Title", "Schema", "Top-level panels", "Datasource variables")
        table.add_row(
            escape(summary.uid),
            escape(summary.title),
            "-" if summary.schema_version is None else str(summary.schema_version),
            str(summary.top_level_panels),
            escape(", ".join(summary.datasource_variables)),
        )
        console.print(table)
    elif to is None:
        # `canonical_json` already ends in a newline, and neither projection
        # goes through `console`: Rich would wrap and highlight a document a
        # caller is piping into `jq` or committing.
        if mode == output_mod.JSON:
            typer.echo(canonical_json(dashboard), nl=False)
        else:
            output_mod.emit(dashboard, mode=output_mod.YAML)


def grafana_dashboard_lint(
    ctx: typer.Context,
    root: RootOption = Path("."),
    path: Annotated[
        list[Path],
        typer.Option(
            "--path",
            help="Specific dashboard JSON file (repeatable). Default: all under the configured chart directory's grafana-dashboards/dashboards/.",
        ),
    ] = [],
    allow_empty: Annotated[
        bool,
        typer.Option(
            "--allow-empty",
            help="Treat 'no dashboards found' as success. For repos or paths that legitimately have no dashboards yet.",
        ),
    ] = False,
    output: DashboardOutputOption = None,
) -> None:
    """Lint Grafana dashboards for repo-wide quality rules.

    `--path` accepts a file or a directory; a directory is linted
    recursively, which is what the default discovery already does.

    `-o table` is one greppable `path: [rule] message` line per finding --
    the shape a human scans and a CI log grep matches. `-o json`/`-o yaml`
    are the same report as the wire document owned by
    `services/grafana/wire.py`, which carries the tally as well as the
    findings so a consumer does not have to count lines.
    """
    from chart_manager.services.grafana.dashboard_lint import (
        discover_dashboards,
        expand_targets,
        lint_paths,
    )
    from chart_manager.services.grafana.wire import lint_result_to_dict

    mode = output_mod.resolve(output, ctx, allowed=_DASHBOARD_OUTPUTS, console=console)
    targets = (
        expand_targets(path)
        if path
        else discover_dashboards(root, charts_dir=Settings().charts_dir)
    )
    if not targets:
        # Linting nothing is not the same as linting clean. A wrong --root, a
        # renamed charts directory, and a --path directory holding no JSON all
        # land here, and exiting 0 made every one of them a silent CI pass.
        # `--allow-empty` is the explicit opt-out for a repo that genuinely
        # has no dashboards yet.
        narration.print("[yellow]no dashboards found[/yellow]")
        raise typer.Exit(code=exit_code_for(Outcome.SUCCESS if allow_empty else Outcome.FAILED))

    result = lint_paths(targets)
    # The findings are this command's report -- its data projection.
    if mode != output_mod.TABLE:
        output_mod.emit(lint_result_to_dict(result), mode=mode)
    else:
        # `typer.echo`, not `console.print`: a finding carries a rule id in
        # square brackets and a message quoting a PromQL expression, so Rich
        # would read `[R002-uid]` as markup and would wrap the long ones --
        # and a wrapped finding is no longer one greppable line.
        for finding in result.findings:
            typer.echo(finding.render())

    # The pass/fail tally narrates the run rather than reporting a finding.
    if not result.ok:
        narration.print(
            f"\n[red]{len(result.findings)} findings across "
            f"{result.files_with_findings}/{result.files_scanned} dashboards[/red]"
        )
        raise typer.Exit(code=exit_code_for(Outcome.FAILED))
    narration.print(f"[green]ok[/green]: {result.files_scanned} dashboards passed")


__all__ = ["register"]
