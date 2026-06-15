"""Progress display for `validate run`.

Three concrete implementations behind a common interface so the runner is
display-agnostic. CLI picks the right one based on --progress (auto, live,
plain, none) + TTY detection + --format. Worker threads call on_event;
displays must be thread-safe (LiveTableDisplay uses an explicit lock).

Design note: stderr-only. Never touch stdout — JSON/markdown output must
remain pipeline-safe (`... --format json | jq ...`).

Wiring example (what the CLI does):

    from chart_manager.services.validate.progress import LiveTableDisplay
    from chart_manager.services.validate.runner import ValidateRunner

    rows = [cfg.row for cfg in row_configs]
    display = LiveTableDisplay()
    display.start(rows)
    try:
        runner = ValidateRunner(
            helm=helm, output_root=out_dir,
            max_workers=8,
            on_event=display.on_event,   # <-- here
        )
        result = runner.run(row_configs)
    finally:
        display.stop()
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

from chart_manager.plumbing.validate_models import WorklistRow

_STATUS_STYLE = {
    "PASS": "green",
    "FAIL": "red",
    "SKIP": "dim",
    "NOT_RUN": "dim",
    "running": "yellow",
}


class ProgressDisplay:
    """Abstract progress display surfaced to the runner via on_event.

    Implementations must be thread-safe: worker threads call on_event
    concurrently. `start` receives the WorklistRows that will be visited
    so the display can pre-size its UI; we accept the row list (not the
    runner's RowConfig) so this module has no upward dependency on the
    runner package.
    """

    def start(self, rows: Sequence[WorklistRow]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def on_event(
        self,
        row: WorklistRow,
        phase: str,
        status: str,
        elapsed_s: float | None = None,
    ) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


class NullDisplay(ProgressDisplay):
    """No-op display for --progress none and JSON output."""

    def start(self, rows: Sequence[WorklistRow]) -> None:
        return

    def on_event(
        self,
        row: WorklistRow,
        phase: str,
        status: str,
        elapsed_s: float | None = None,
    ) -> None:
        return

    def stop(self) -> None:
        return


class PlainNarrationDisplay(ProgressDisplay):
    """One stderr line per phase-end event.

    Format: `[done/total] chart/env phase…STATUS (1.4s)`. Suitable for
    non-TTY logs (CI without colors, file redirection) and for --verbose
    where Live would fight with subprocess stdout streaming.
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console(file=sys.stderr, force_terminal=False, no_color=True)
        self._total = 0
        self._done = 0
        self._lock = threading.Lock()
        # Track rows we've already counted as done so render/schema/policy
        # events from the same row don't triple-bump the counter.
        self._row_done: set[tuple[str, str]] = set()

    def start(self, rows: Sequence[WorklistRow]) -> None:
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
        if status == "running":
            return  # narration prints on phase end only
        with self._lock:
            key = (row.chart, row.env)
            if phase == "policy" and key not in self._row_done:
                self._row_done.add(key)
                self._done += 1
            counter = f"[{self._done}/{self._total}]"
            suffix = f" ({elapsed_s:.1f}s)" if elapsed_s is not None else ""
            self.console.print(
                f"{counter} {row.chart}/{row.env} {phase}…{status}{suffix}"
            )

    def stop(self) -> None:
        return


class LiveTableDisplay(ProgressDisplay):
    """Rich Live table; cells update from `…` to status as phases complete.

    Worker threads call `on_event` concurrently; an explicit lock guards
    the Table's internal columns list so concurrent mutations don't
    corrupt Rich's render state.
    """

    def __init__(self, console: Console | None = None, refresh_per_second: int = 10) -> None:
        self.console = console or Console(file=sys.stderr)
        self.refresh_per_second = refresh_per_second
        self._live: Live | None = None
        self._lock = threading.Lock()
        # (chart, env) -> row index in the table
        self._index: dict[tuple[str, str], int] = {}
        # per-row start time (first 'running' event) for elapsed display
        self._row_start: dict[tuple[str, str], float] = {}
        # per-(row, phase) status cache; needed to rebuild rows since
        # rich.table.Table doesn't expose a per-cell setter — we rebuild
        # the row each update.
        self._cells: dict[tuple[str, str], dict[str, str]] = {}

    def start(self, rows: Sequence[WorklistRow]) -> None:
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
                "elapsed": "",
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
        elapsed_s: float | None = None,
    ) -> None:
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
                    cell["elapsed"] = f"{time.monotonic() - self._row_start[key]:.1f}s"
            self._rebuild_table()

    def stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _build_table(self, rows: Sequence[WorklistRow]) -> Table:
        table = Table(
            "Chart",
            "Env",
            "Render",
            "Schema",
            "Policy",
            "Elapsed",
            title="validate (running)",
        )
        for row in rows:
            table.add_row(row.chart, row.env, "…", "…", "…", "")
        return table

    def _rebuild_table(self) -> None:
        # Caller holds self._lock.
        new_table = Table(
            "Chart",
            "Env",
            "Render",
            "Schema",
            "Policy",
            "Elapsed",
            title="validate (running)",
        )
        # Iterate in original insertion order for stable display.
        for key, _idx in sorted(self._index.items(), key=lambda kv: kv[1]):
            cell = self._cells[key]
            new_table.add_row(
                cell["chart"],
                cell["env"],
                _styled(cell["render"]),
                _styled(cell["schema"]),
                _styled(cell["policy"]),
                Text(cell["elapsed"], style="dim"),
            )
        if self._live is not None:
            self._live.update(new_table)


def _styled(status: str) -> Text:
    style = _STATUS_STYLE.get(status, "")
    return Text(status, style=style)
