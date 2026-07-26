"""Terminal renderers and live progress driver for `helmrelease monitor/test`.

Module-level functions, no Renderer protocol/ABC -- the CLI handler picks one
of four functions based on (command, mode). _PrettyProgressDriver is the
only stateful piece, used as a context manager during pretty runs to hold a
Rich Live table; thread-safe under the monitor/test executor.

Everything here is terminal-shaped: Rich tables, color styles, panels, and
the encoder settings for the CLI's JSON stream. The *payload* those JSON
writers emit is not defined here -- it is a versioned wire contract owned by
`services.helmrelease.wire`, so an HTTP/Slack/RPC surface can return the same
bytes without importing anything under `cli/`.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import IO, Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from chart_manager.services.helmrelease import (
    NO_MATCH_REF,
    PASSING_VERDICTS,
    HelmReleaseRef,
    MonitorResult,
    TestResult,
    Transition,
    monitor_to_dict,
    test_to_dict,
)

_LOG = logging.getLogger(__name__)

# Encoder settings for the JSON stream. Compact + sorted keys makes the
# output diffable and jq-friendly; `default=str` is a backstop for any
# stray non-JSON scalar the wire layer did not stringify.
_JSON_DUMP_KWARGS: dict[str, Any] = {
    "sort_keys": True,
    "separators": (",", ":"),
    "default": str,
}


def _fmt_duration(seconds: float) -> str:
    """Format seconds compactly: 1.2s, 3m04s, 1h02m03s."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{sec:02d}s"


def _summary_line(*, ok_count: int, total: int, duration: float) -> str:
    """Build the 'N/M ready in Xs' headline string."""
    return f"{ok_count}/{total} ready in {_fmt_duration(duration)}"


def render_monitor_pretty(
    result: MonitorResult,
    console: Console,
    *,
    chart: str,
    version: str,
) -> None:
    """Render monitor results as headline + table + failure panels."""
    # NO_MATCH_REF is a sentinel outcome meaning zero HRs matched; identity
    # check drops it from the table.
    real_outcomes = tuple(o for o in result.outcomes if o.ref is not NO_MATCH_REF)
    if not real_outcomes:
        console.print(
            f"[yellow]no helmreleases matched[/yellow] chart={chart} version={version}"
        )
        return

    # Imported, not re-stated: `result.ok` (which drives the exit code) folds
    # the same set. Hardcoding the tuple here is how the headline count and
    # the exit code came to be able to disagree about a newly added verdict.
    ok_count = sum(1 for o in real_outcomes if o.verdict in PASSING_VERDICTS)
    summary = _summary_line(
        ok_count=ok_count, total=len(real_outcomes), duration=result.total_duration_seconds
    )
    headline_style = "green" if result.ok else "red"
    console.print(f"[{headline_style}]{summary}[/{headline_style}]  chart={chart}@{version}")

    table = Table("Namespace", "Name", "Verdict", "Duration", "Ready Reason")
    for o in real_outcomes:
        ready_reason = ""
        if o.last_status and o.last_status.ready:
            ready_reason = o.last_status.ready.reason
        style = _verdict_style(o.verdict)
        table.add_row(
            o.ref.namespace,
            o.ref.name,
            f"[{style}]{o.verdict}[/{style}]",
            _fmt_duration(o.duration_seconds),
            ready_reason,
        )
    console.print(table)

    for o in real_outcomes:
        if o.verdict in PASSING_VERDICTS:
            continue
        if not o.diagnostics:
            continue
        recent = o.recent_transitions[-3:]
        body = o.diagnostics
        if recent:
            body += "\n\n--- last transitions ---\n"
            body += "\n".join(
                f"{t.at.isoformat()} {t.phase} - {t.detail}" for t in recent
            )
        console.print(
            Panel(body, title=f"{o.ref.namespace}/{o.ref.name} [{o.verdict}]", border_style="red")
        )


def render_monitor_json(
    result: MonitorResult,
    file: IO[str],
    *,
    chart: str,
    version: str,
) -> None:
    """Write the monitor result as a single JSON line to `file`.

    Transport only: the payload comes from `services.helmrelease.wire`.
    """
    json.dump(monitor_to_dict(result, chart=chart, version=version), file, **_JSON_DUMP_KWARGS)
    file.write("\n")
    file.flush()


def render_test_pretty(
    result: TestResult,
    console: Console,
    *,
    chart: str,
    version: str,
) -> None:
    """Render test results as headline + table + failure panels."""
    # Same NO_MATCH_REF sentinel filtering as render_monitor_pretty.
    real_outcomes = tuple(o for o in result.outcomes if o.ref is not NO_MATCH_REF)
    if not real_outcomes:
        console.print(
            f"[yellow]no helmreleases matched[/yellow] chart={chart} version={version}"
        )
        return

    ok_count = sum(1 for o in real_outcomes if o.verdict in PASSING_VERDICTS)
    headline_style = "green" if result.ok else "red"
    summary = (
        f"{ok_count}/{len(real_outcomes)} passed in "
        f"{_fmt_duration(result.total_duration_seconds)}"
    )
    console.print(f"[{headline_style}]{summary}[/{headline_style}]  chart={chart}@{version}")

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
        if o.verdict in PASSING_VERDICTS:
            continue
        if not o.diagnostics:
            continue
        recent = o.phase_log[-3:]
        body = o.diagnostics
        if recent:
            body += "\n\n--- phase log ---\n"
            body += "\n".join(
                f"{t.at.isoformat()} {t.phase} - {t.detail}" for t in recent
            )
        console.print(
            Panel(body, title=f"{o.ref.namespace}/{o.ref.name} [{o.verdict}]", border_style="red")
        )


def render_test_json(
    result: TestResult,
    file: IO[str],
    *,
    chart: str,
    version: str,
) -> None:
    """Write the test result as a single JSON line to `file`.

    Transport only: the payload comes from `services.helmrelease.wire`.
    """
    json.dump(test_to_dict(result, chart=chart, version=version), file, **_JSON_DUMP_KWARGS)
    file.write("\n")
    file.flush()


def _verdict_style(verdict: str) -> str:
    """Map a verdict string to a Rich color style (unknown verdicts are red)."""
    if verdict in ("ready", "passed"):
        return "green"
    if verdict in ("skipped-suspended", "skipped-not-ready"):
        return "yellow"
    if verdict == "no-match":
        return "yellow"
    return "red"


class _PrettyProgressDriver:
    """Thread-safe live progress driver. Used as a context manager.

    Holds a Rich Live table that re-renders per-HR transitions. The lock
    guards both the per-HR state map and the Live.update call so concurrent
    worker threads cannot interleave updates. Render exceptions are caught
    and logged -- a render bug must never break the underlying run.
    """

    def __init__(self, console: Console) -> None:
        """Prepare state; the Live table itself is created in __enter__."""
        self._console = console
        self._lock = threading.Lock()
        self._state: dict[tuple[str, str], Transition] = {}
        # Lazy-imported so importing helmrelease_render in non-pretty paths
        # (CI logs, tests) doesn't drag rich.live into the process.
        from rich.live import Live

        self._Live = Live
        self._live: Any | None = None

    def __enter__(self) -> _PrettyProgressDriver:
        """Start the Rich Live table."""
        self._live = self._Live(
            self._render(),
            console=self._console,
            auto_refresh=True,
            refresh_per_second=4,
        )
        self._live.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop the Live table."""
        if self._live is not None:
            self._live.__exit__(*exc)
            self._live = None

    def __call__(self, ref: HelmReleaseRef, transition: Transition) -> None:
        """Progress callback: record the latest transition and refresh the table."""
        try:
            with self._lock:
                self._state[(ref.namespace, ref.name)] = transition
                if self._live is not None:
                    self._live.update(self._render())
        except Exception:
            _LOG.exception("progress driver update failed")

    def _render(self) -> Table:
        """Build the current progress table, sorted by (namespace, name)."""
        table = Table("Namespace", "Name", "Phase", "Detail")
        for (ns, name) in sorted(self._state.keys()):
            t = self._state[(ns, name)]
            table.add_row(ns, name, t.phase, t.detail)
        return table
