"""The progress sink: a live table at a terminal, one line per event elsewhere.

Everything goes to `streams.narration`, so `-q` silences it. Text is escaped: it
carries subprocess output, and an unmatched tag such as `[/etc/hosts]` raises
Rich's MarkupError.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Group, RenderableType
from rich.live import Live
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from chart_manager.cli import streams
from chart_manager.plumbing.progress import Progress, ProgressEvent, RowUpdate

_SEVERITY_STYLES = {"step": "bold", "detail": "dim", "warn": "yellow", "error": "red"}
_STATUS_STYLES = {
    "running": "yellow",
    "passed": "green",
    "failed": "red",
    "error": "bold red",
    "skipped": "dim",
}

_Cells = dict[tuple[str, ...], dict[str, RowUpdate]]


@contextmanager
def progress_view(*, live: bool) -> Iterator[Progress]:
    """Yield the `Progress` a command hands its producers.

    With `live`, `RowUpdate`s draw a table with one row per key and the latest status
    per cell, and `ProgressEvent` lines print above it. Producers may call from
    worker threads.
    """
    if not live or streams.narration.quiet:
        yield print_progress
        return
    cells: _Cells = {}
    lock = threading.Lock()
    with Live(_table(cells), console=streams.narration, refresh_per_second=4) as view:

        def show(event: ProgressEvent | RowUpdate) -> None:
            with lock:
                if isinstance(event, ProgressEvent):
                    view.console.print(_line(event))
                    return
                cells.setdefault(event.key, {})[event.column] = event
                view.update(_table(cells))

        yield show


def print_progress(event: ProgressEvent | RowUpdate) -> None:
    """Print one event as one line on the narration console."""
    streams.narration.print(_line(event))


def _line(event: ProgressEvent | RowUpdate) -> str:
    if isinstance(event, RowUpdate):
        words = ("/".join(event.key), f"{event.column}:", event.status, event.detail)
        return escape(" ".join(word for word in words if word))
    style = _SEVERITY_STYLES.get(event.severity)
    message = escape(event.message)
    if event.label is None:
        return f"[{style}]{message}[/{style}]" if style else message
    label = f"[{style}]{escape(event.label)}[/{style}]" if style else escape(event.label)
    return f"{label} {message}".rstrip()


def _table(cells: _Cells) -> RenderableType:
    if not cells:
        return Group()
    columns = list(dict.fromkeys(column for row in cells.values() for column in row))
    table = Table("", *columns)
    for key, row in cells.items():
        table.add_row(Text("/".join(key)), *(_cell(row.get(column)) for column in columns))
    return table


def _cell(update: RowUpdate | None) -> Text:
    if update is None:
        return Text("")
    text = Text(update.status, style=_STATUS_STYLES.get(update.status, ""))
    if update.detail:
        text.append(f" {update.detail}", style="dim")
    return text
