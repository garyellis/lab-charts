"""Terminal progress for `chart validate`: a live table, or one line per finished check."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Sequence

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from chart_manager.commands.validate.models import Row

#: Rich styles for each status a cell can show.
STATUS_STYLE = {
    "running": "yellow",
    "passed": "green",
    "failed": "red",
    "error": "bold red",
    "skipped": "dim",
}
_LIVE_COLUMNS = ("Chart", "Env", "Render", "Schema", "Policy", "Wall")


class PlainNarration:
    """One stderr line per finished check: `[n/total] chart/env check…status (1.4s)`.

    For logs without a terminal and for --verbose, where a live table would fight with
    streamed subprocess output.
    """

    def __init__(self) -> None:
        self.console = Console(file=sys.stderr, force_terminal=False, no_color=True)
        self._position: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def start(self, rows: Sequence[Row]) -> None:
        self._position = {(row.chart, row.env): index for index, row in enumerate(rows, 1)}

    def on_event(self, row: Row, check: str, status: str, elapsed_s: float | None = None) -> None:
        if status == "running":
            return
        counter = f"[{self._position.get((row.chart, row.env), 0)}/{len(self._position)}]"
        suffix = f" ({elapsed_s:.1f}s)" if elapsed_s is not None else ""
        with self._lock:
            self.console.print(f"{counter} {row.chart}/{row.env} {check}…{status}{suffix}")

    def stop(self) -> None:
        pass


class LiveTable:
    """A Rich Live table on stderr whose cells change from `…` to each check's status."""

    def __init__(self) -> None:
        self.console = Console(file=sys.stderr)
        self._live: Live | None = None
        self._lock = threading.Lock()
        self._cells: dict[tuple[str, str], dict[str, str]] = {}
        self._started: dict[tuple[str, str], float] = {}

    def start(self, rows: Sequence[Row]) -> None:
        self._cells = {
            (row.chart, row.env): {"render": "…", "schema": "…", "policy": "…", "wall": ""}
            for row in rows
        }
        self._live = Live(self._table(), console=self.console, refresh_per_second=10)
        self._live.start()

    def on_event(
        self,
        row: Row,
        check: str,
        status: str,
        elapsed_s: float | None = None,  # noqa: ARG002 -- the Wall column times the whole row
    ) -> None:
        key = (row.chart, row.env)
        with self._lock:
            if key not in self._cells:
                return
            cells = self._cells[key]
            cells[check] = status
            if status == "running":
                self._started.setdefault(key, time.monotonic())
            elif key in self._started:
                cells["wall"] = f"{time.monotonic() - self._started[key]:.1f}s"
            if self._live is not None:
                self._live.update(self._table())

    def stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _table(self) -> Table:
        table = Table(*_LIVE_COLUMNS, title="validate (running)")
        for (chart, env), cells in self._cells.items():
            table.add_row(
                chart,
                env,
                *(Text(cells[name], style=STATUS_STYLE.get(cells[name], "")) for name in _CHECKS),
                Text(cells["wall"], style="dim"),
            )
        return table


_CHECKS = ("render", "schema", "policy")
