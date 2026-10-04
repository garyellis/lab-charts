"""`chart validate`'s output: the terminal table, markdown and JSON forms of one run."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import get_args

from rich.table import Table
from rich.text import Text

from chart_manager.commands.validate.display import STATUS_STYLE
from chart_manager.commands.validate.models import (
    FAILING,
    CheckName,
    CheckResult,
    Diagnostics,
    Row,
    ValidateOutcome,
)
from chart_manager.plumbing.exit_codes import exit_code_for

_CHECKS: tuple[CheckName, ...] = get_args(CheckName)
_EMOJI = {"passed": "✅", "failed": "❌", "error": "⚠️", "skipped": "➖"}  # noqa: RUF001
_NOT_RUN = "·"


def to_json(outcome: ValidateOutcome, *, rendered: Path) -> dict[str, object]:
    """The run as a jq-friendly dict; `elapsed_seconds` is always present."""
    passing, failing, _ = _tally(outcome)
    return {
        "exit_code": exit_code_for(outcome.outcome()),
        "rendered_root": str(rendered),
        "summary": {
            "rows": len(outcome.rows),
            "passing_rows": passing,
            "failing_rows": failing,
            "spec_errors": len(outcome.spec_errors),
        },
        "rows": [
            {
                "chart": row.chart,
                "env": row.env,
                "release": row.release,
                "namespace": row.namespace,
                "checks": {
                    name: {
                        "status": result.status,
                        "detail": result.detail,
                        "elapsed_seconds": (
                            None
                            if result.elapsed_seconds is None
                            else round(result.elapsed_seconds, 3)
                        ),
                    }
                    for name, result in row.checks.items()
                },
            }
            for row in outcome.rows
        ],
        "spec_errors": list(outcome.spec_errors),
        "warnings": list(outcome.warnings),
    }


def to_markdown(outcome: ValidateOutcome, *, timings: bool) -> str:
    """The run as GitHub-flavoured markdown: table, tally, details, diagnostics and warnings."""
    lines = ["## validate", ""]
    if not outcome.rows:
        lines.append(f"_nothing to validate: {_no_work_reason(outcome.diagnostics)}_")
    else:
        header = ["Chart", "Env", "Release", "Render", "Schema", "Policy"]
        header += ["Elapsed"] if timings else []
        lines += ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
        for row in outcome.rows:
            cells = [row.chart, row.env, row.release]
            cells += [_EMOJI.get(_status(row, name), _NOT_RUN) for name in _CHECKS]
            cells += [_elapsed(row)] if timings else []
            lines.append("| " + " | ".join(cells) + " |")
        passing, failing, skipped = _tally(outcome)
        lines += [
            "",
            f"**{len(outcome.rows)} rows · {passing} passing · {failing} failing · "
            f"{skipped} skipped**",
        ]
        for title, wanted in (("Failures", FAILING), ("Advisories", {"passed"})):
            blocks = [
                _details(f"{row.chart}/{row.env} — {name}", result.detail)
                for row, name, result in _checks(outcome)
                if result.status in wanted and result.detail
            ]
            if blocks:
                lines += ["", f"### {title}", ""]
                for block in blocks:
                    lines += [*block, ""]
    diagnostics = _diagnostics(outcome.diagnostics)
    if diagnostics:
        lines += ["", "### Diagnostics", "", *diagnostics]
    notes = [f"- {warning}" for warning in outcome.warnings]
    if outcome.spec_errors:
        notes.append(f"- {len(outcome.spec_errors)} spec error(s):")
        notes += [f"  - {error}" for error in outcome.spec_errors]
    if notes:
        lines += ["", "### Warnings", "", *notes]
    return "\n".join(lines).rstrip() + "\n"


def _no_work_reason(diagnostics: Diagnostics) -> str:
    """Why a run had no rows, most specific cause first."""
    if diagnostics.requested_charts or diagnostics.requested_envs:
        return "requested filters selected no affected validation cases"
    if diagnostics.unmatched_changes:
        return "changed files matched no validation trigger"
    if diagnostics.ignored_changes:
        return "all relevant changed files were explicitly ignored"
    if diagnostics.charts_unvalidated:
        return "no chart with manifest-validation configuration was selected"
    return "no affected validation cases"


def _diagnostics(diagnostics: Diagnostics) -> list[str]:
    """Markdown bullets for the filters asked for and what the selection left out."""
    lines = []
    if diagnostics.requested_charts:
        lines.append(f"- Requested charts: {', '.join(diagnostics.requested_charts)}")
    if diagnostics.requested_envs:
        lines.append(f"- Requested environments: {', '.join(diagnostics.requested_envs)}")
    if diagnostics.ignored_changes:
        lines += ["- Ignored changes:", *(f"  - `{path}`" for path in diagnostics.ignored_changes)]
    if diagnostics.unmatched_changes:
        lines.append("- Changes matching no trigger:")
        lines += [f"  - `{path}`" for path in diagnostics.unmatched_changes]
    if diagnostics.rows_filtered_out:
        lines.append(f"- Rows filtered out: {diagnostics.rows_filtered_out}")
    if diagnostics.charts_unvalidated:
        lines.append(
            "- Charts without manifest-validation configuration: "
            f"{diagnostics.charts_unvalidated}"
        )
    return lines


def to_table(outcome: ValidateOutcome, *, timings: bool) -> Table:
    """The run as a Rich table for the terminal."""
    columns = ["Chart", "Env", "Release", "Render", "Schema", "Policy"]
    table = Table(*columns, *(["Elapsed"] if timings else []), title="validate")
    for row in outcome.rows:
        cells: list[str | Text] = [row.chart, row.env, row.release]
        for name in _CHECKS:
            status = _status(row, name)
            cells.append(Text(status or "-", style=STATUS_STYLE.get(status, "dim")))
        if timings:
            cells.append(Text(_elapsed(row), style="dim"))
        table.add_row(*cells)
    return table


def details(outcome: ValidateOutcome) -> list[str]:
    """Rich-markup blocks to print under the table: failures and errors, then advisories."""
    failures = [
        f"[red]{row.chart}/{row.env}[/red] [bold]{name}[/bold]\n{result.detail}".rstrip()
        for row, name, result in _checks(outcome)
        if result.status in FAILING
    ]
    advisories = [
        f"[yellow]{row.chart}/{row.env}[/yellow] [bold]{name}[/bold]\n{result.detail}"
        for row, name, result in _checks(outcome)
        if result.status == "passed" and result.detail
    ]
    return failures + advisories


def _checks(outcome: ValidateOutcome) -> Iterator[tuple[Row, str, CheckResult]]:
    for row in outcome.rows:
        for name in _CHECKS:
            if name in row.checks:
                yield row, name, row.checks[name]


def _status(row: Row, name: str) -> str:
    result = row.checks.get(name)  # type: ignore[call-overload]
    return "" if result is None else result.status


def _elapsed(row: Row) -> str:
    timed = [r.elapsed_seconds for r in row.checks.values() if r.elapsed_seconds is not None]
    return f"{sum(timed):.1f}s" if timed else ""


def _tally(outcome: ValidateOutcome) -> tuple[int, int, int]:
    """Passing, failing and skipped rows: any failure or error fails a row."""
    passing = failing = skipped = 0
    for row in outcome.rows:
        statuses = {result.status for result in row.checks.values()}
        if statuses & FAILING:
            failing += 1
        elif statuses <= {"skipped"}:
            skipped += 1
        else:
            passing += 1
    return passing, failing, skipped


def _details(summary: str, detail: str) -> list[str]:
    """A `<details>` block whose summary is HTML-escaped and whose fence outlasts the body's."""
    escaped = summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    fence = "`" * max(3, _longest_backticks(detail) + 1)
    return [
        f"<details><summary>{escaped}</summary>",
        "",
        fence,
        detail.rstrip(),
        fence,
        "",
        "</details>",
    ]


def _longest_backticks(body: str) -> int:
    longest = run = 0
    for char in body:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    return longest
