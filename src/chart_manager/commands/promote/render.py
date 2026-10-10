"""Terminal renderers for `promote monitor/test`.

Module-level functions, no Renderer protocol/ABC.

Everything here is terminal-shaped: Rich tables, color styles and panels.
The json form of each result is its document (`plumbing.documents.to_document`).
"""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from chart_manager.commands.promote.monitor import MonitorResult
from chart_manager.commands.promote.state import NO_MATCH_REF, PASSING_VERDICTS
from chart_manager.commands.promote.test import TestResult


def _fmt_duration(seconds: float) -> str:
    """Format seconds compactly: 1.2s, 3m04s, 1h02m03s."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{sec:02d}s"


def render_pretty(result: MonitorResult | TestResult, console: Console, *, noun: str) -> None:
    """Render a monitor or test result as headline + table + failure panels.

    `noun` names a passing release in the headline: "ready" or "passed".
    """
    # NO_MATCH_REF is a sentinel outcome meaning zero HRs matched; identity
    # check drops it from the table.
    real_outcomes = tuple(o for o in result.outcomes if o.ref is not NO_MATCH_REF)
    if not real_outcomes:
        console.print(
            f"[yellow]no helmreleases matched[/yellow] chart={result.chart} "
            f"version={result.version}"
        )
        return

    # Imported, not re-stated: `result.ok` (which drives the exit code) folds
    # the same set, so the headline count and the exit code cannot disagree.
    ok_count = sum(1 for o in real_outcomes if o.verdict in PASSING_VERDICTS)
    summary = (
        f"{ok_count}/{len(real_outcomes)} {noun} in "
        f"{_fmt_duration(result.total_duration_seconds)}"
    )
    headline_style = "green" if result.ok else "red"
    subject = f"chart={result.chart}@{result.version}"
    console.print(f"[{headline_style}]{summary}[/{headline_style}]  {subject}")

    table = Table("Namespace", "Name", "Verdict", "Duration", "Reason")
    for o in real_outcomes:
        style = _verdict_style(o.verdict)
        table.add_row(
            o.ref.namespace,
            o.ref.name,
            f"[{style}]{o.verdict}[/{style}]",
            _fmt_duration(o.duration_seconds),
            o.reason,
        )
    console.print(table)

    for o in real_outcomes:
        if o.verdict in PASSING_VERDICTS or not o.diagnostics:
            continue
        recent = o.transitions[-3:]
        body = o.diagnostics
        if recent:
            body += "\n\n--- last transitions ---\n"
            body += "\n".join(f"{t.at.isoformat()} {t.phase} - {t.detail}" for t in recent)
        console.print(
            Panel(body, title=f"{o.ref.namespace}/{o.ref.name} [{o.verdict}]", border_style="red")
        )


def _verdict_style(verdict: str) -> str:
    """Map a verdict string to a Rich color style (unknown verdicts are red)."""
    if verdict in ("ready", "passed"):
        return "green"
    if verdict in ("skipped-suspended", "skipped-not-ready"):
        return "yellow"
    if verdict == "no-match":
        return "yellow"
    return "red"
