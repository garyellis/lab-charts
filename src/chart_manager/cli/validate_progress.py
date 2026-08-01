"""Rich adapters for the validate progress port.

Terminal implementations of `services/manifest_validation/progress.ProgressDisplay`:
a live repainting table for interactive use and a one-line-per-event
narrator for logs. They change how a run *looks*, never what it produces —
which is why they sit here and not in `services/`.

Both write to stderr only, so `-o json` stays pipeline-safe on
stdout. Worker threads call `on_event` concurrently; `LiveTableDisplay`
holds an explicit lock, `PlainNarrationDisplay` guards its counters.

`cli/validate.py:_resolve_display` picks between these and `NullDisplay`
from --progress + --output + TTY status.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Sequence

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from chart_manager.cli.validate_render import STATUS_STYLE
from chart_manager.services.manifest_validation.models import WorklistRow
from chart_manager.services.manifest_validation.progress import ProgressDisplay

__all__ = ["LiveTableDisplay", "PlainNarrationDisplay"]

# Extend rather than redeclare: the live table shows one status the final
# table never sees ("running", between a phase's start and end events), but
# PASS/FAIL/SKIP/NOT_RUN must stay in lockstep with `to_text_table` — a
# second copy of those four is how they drift.
_STATUS_STYLE = {**STATUS_STYLE, "running": "yellow"}

#: Live-table columns. The last one is "Wall", NOT "Elapsed", and the
#: difference is load-bearing: this column is wall-clock since the row's
#: first `running` event, whereas the "Elapsed" column in the final table
#: (`validate_render.to_text_table` via `wire.row_elapsed_text`) sums the
#: row's measured per-phase times. Under `--workers > 1` a row spends real
#: wall-clock time waiting for a worker, so the two numbers genuinely
#: diverge. They previously shared the header "Elapsed", which read as a
#: bug in one of them.
_LIVE_COLUMNS = ("Chart", "Env", "Render", "Schema", "Policy", "Wall")


class PlainNarrationDisplay(ProgressDisplay):
    """One stderr line per phase-end event.

    Format: `[done/total] chart/env phase…STATUS (1.4s)`. Suitable for
    non-TTY logs (CI without colors, file redirection) and for --verbose
    where Live would fight with subprocess stdout streaming.
    """

    def __init__(self, console: Console | None = None) -> None:
        """Set up a stderr, no-color console and the done/total counters."""
        self.console = console or Console(file=sys.stderr, force_terminal=False, no_color=True)
        self._total = 0
        self._done = 0
        self._lock = threading.Lock()
        # Track rows we've already counted as done so render/schema/policy
        # events from the same row don't triple-bump the counter.
        self._row_done: set[tuple[str, str]] = set()

    def start(self, rows: Sequence[WorklistRow]) -> None:
        """Reset counters for a fresh run of `rows`."""
        self._total = len(rows)
        self._done = 0
        self._row_done.clear()

    def on_event(
        self,
        row: WorklistRow,
        phase: str,
        status: str,
        elapsed_s: float | None = None,
    ) -> None:
        """Print one line per phase end; bump the done counter once per row (on policy)."""
        if status == "running":
            return  # narration prints on phase end only
        with self._lock:
            key = (row.chart, row.env)
            if phase == "policy" and key not in self._row_done:
                self._row_done.add(key)
                self._done += 1
            counter = f"[{self._done}/{self._total}]"
            suffix = f" ({elapsed_s:.1f}s)" if elapsed_s is not None else ""
            self.console.print(f"{counter} {row.chart}/{row.env} {phase}…{status}{suffix}")

    def stop(self) -> None:
        """No-op (nothing persistent to tear down)."""
        return


class LiveTableDisplay(ProgressDisplay):
    """Rich Live table; cells update from `…` to status as phases complete.

    Worker threads call `on_event` concurrently; an explicit lock guards
    the Table's internal columns list so concurrent mutations don't
    corrupt Rich's render state.
    """

    def __init__(self, console: Console | None = None, refresh_per_second: int = 10) -> None:
        """Set up the stderr console and per-row/per-cell tracking state."""
        self.console = console or Console(file=sys.stderr)
        self.refresh_per_second = refresh_per_second
        self._live: Live | None = None
        self._lock = threading.Lock()
        # (chart, env) -> row index in the table
        self._index: dict[tuple[str, str], int] = {}
        # per-row start time (first 'running' event) for the Wall column
        self._row_start: dict[tuple[str, str], float] = {}
        # per-(row, phase) status cache; needed to rebuild rows since
        # rich.table.Table doesn't expose a per-cell setter — we rebuild
        # the row each update.
        self._cells: dict[tuple[str, str], dict[str, str]] = {}

    def start(self, rows: Sequence[WorklistRow]) -> None:
        """Build the table, seed per-row cells to `…`, and start the Live render."""
        table = self._build_table(rows)
        for idx, row in enumerate(rows):
            key = (row.chart, row.env)
            self._index[key] = idx
            self._cells[key] = {
                "chart": row.chart,
                "env": row.env,
                "render": "…",
                "schema": "…",
                "policy": "…",
                "wall": "",
            }
        self._live = Live(
            table,
            console=self.console,
            refresh_per_second=self.refresh_per_second,
            transient=False,
        )
        self._live.start()

    def on_event(
        self,
        row: WorklistRow,
        phase: str,
        status: str,
        # Protocol arg: the table's wall column is row wall-clock measured
        # here across phases, not the per-phase elapsed the runner reports.
        elapsed_s: float | None = None,  # noqa: ARG002
    ) -> None:
        """Update the row's phase cell and re-render, under the lock."""
        key = (row.chart, row.env)
        with self._lock:
            if key not in self._cells:
                return
            cell = self._cells[key]
            if status == "running":
                self._row_start.setdefault(key, time.monotonic())
                cell[phase] = "running"
            else:
                cell[phase] = status
                if key in self._row_start:
                    cell["wall"] = f"{time.monotonic() - self._row_start[key]:.1f}s"
            self._rebuild_table()

    def stop(self) -> None:
        """Stop the Live render, leaving the final table on screen."""
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _build_table(self, rows: Sequence[WorklistRow]) -> Table:
        """Build the initial table with all cells at `…`."""
        table = Table(*_LIVE_COLUMNS, title="validate (running)")
        for row in rows:
            table.add_row(row.chart, row.env, "…", "…", "…", "")
        return table

    def _rebuild_table(self) -> None:
        """Rebuild and push the whole table from cached cells (Rich has no per-cell setter)."""
        # Caller holds self._lock.
        new_table = Table(*_LIVE_COLUMNS, title="validate (running)")
        # Iterate in original insertion order for stable display.
        for key, _idx in sorted(self._index.items(), key=lambda kv: kv[1]):
            cell = self._cells[key]
            new_table.add_row(
                cell["chart"],
                cell["env"],
                _styled(cell["render"]),
                _styled(cell["schema"]),
                _styled(cell["policy"]),
                Text(cell["wall"], style="dim"),
            )
        if self._live is not None:
            self._live.update(new_table)


def _styled(status: str) -> Text:
    """Wrap a status string in its Rich color style."""
    style = _STATUS_STYLE.get(status, "")
    return Text(status, style=style)
