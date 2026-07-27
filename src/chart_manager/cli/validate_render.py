"""Terminal renderers for `validate` results.

Everything here is terminal-shaped and would be *necessarily different* on
another surface: Rich `Table`/`Text` widgets, color styles, and strings
carrying Rich console markup (`[red]...[/red]`) that only mean anything to a
`rich.Console`. An HTTP server, a Slack app, or a PR-comment bot has no
terminal and must not import this module.

The machine-readable projections -- `to_json`, `to_markdown`,
`JSON_SCHEMA_VERSION` -- live in `services.manifest_validation.wire` and import no Rich.

Note on `failure_details` / `advisory_details`: these return strings
containing Rich markup, which is why they live here rather than in the wire
module. The markup is emphatically *not* part of the wire contract. No
information is lost by keeping them terminal-only: the same failure and
advisory data is already carried by `wire.to_json` (`rows[].phases[].detail`
/ `.artifacts`) and by the Failures/Advisories sections of
`wire.to_markdown`, both markup-free.
"""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from chart_manager.services.manifest_validation.models import PhaseResult, RunResult
from chart_manager.services.manifest_validation.wire import row_elapsed_text

#: Rich style per terminal phase status. Shared with `cli/validate_progress.py`
#: so the live table and the final table can never disagree about what a FAIL
#: looks like — they used to hold separate copies of this map.
STATUS_STYLE = {
    "PASS": "green",
    "FAIL": "red",
    "SKIP": "dim",
    "NOT_RUN": "dim",
}


def to_text_table(result: RunResult, *, include_timings: bool = False) -> Table:
    """Render a RunResult as a Rich table for terminal output."""
    columns = ["Chart", "Env", "Release", "Render", "Schema", "Policy"]
    if include_timings:
        columns.append("Elapsed")
    table = Table(*columns, title="validate")
    for row_result in result.rows:
        cells: list[str | Text] = [
            row_result.row.chart,
            row_result.row.env,
            row_result.row.release,
            _cell(row_result.phases.get("render")),
            _cell(row_result.phases.get("schema")),
            _cell(row_result.phases.get("policy")),
        ]
        if include_timings:
            cells.append(Text(row_elapsed_text(row_result), style="dim"))
        table.add_row(*cells)
    return table


def failure_details(result: RunResult) -> list[str]:
    """One block per failed phase, suitable for printing under the table.

    Returns Rich console markup — pass to `Console.print`, never to a JSON
    body or a webhook payload.
    """
    blocks: list[str] = []
    for row_result in result.rows:
        for phase_name, phase in row_result.phases.items():
            if phase.status != "FAIL":
                continue
            detail = phase.detail or ""
            header = (
                f"[red]{row_result.row.chart}/{row_result.row.env}[/red] [bold]{phase_name}[/bold]"
            )
            artifacts = "\n".join(f"  artifact: {a}" for a in phase.artifacts)
            block = header + ("\n" + detail if detail else "")
            if artifacts:
                block += "\n" + artifacts
            blocks.append(block)
    return blocks


def advisory_details(result: RunResult) -> list[str]:
    """One block per PASS phase that carries advisory detail (e.g. kyverno warns).

    Returns Rich console markup — see `failure_details`.
    """
    blocks: list[str] = []
    for row_result in result.rows:
        for phase_name, phase in row_result.phases.items():
            if phase.status != "PASS" or not phase.detail:
                continue
            header = (
                f"[yellow]{row_result.row.chart}/{row_result.row.env}[/yellow] "
                f"[bold]{phase_name}[/bold]"
            )
            blocks.append(header + "\n" + phase.detail)
    return blocks


def _cell(phase: PhaseResult | None) -> Text:
    """Style one status cell; dim dash when the phase is absent."""
    if phase is None:
        return Text("-", style="dim")
    style = STATUS_STYLE.get(phase.status, "")
    return Text(phase.status, style=style)
