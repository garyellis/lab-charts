"""`progress_view`: every progress event reaches the narration console, as a line or a table."""

from __future__ import annotations

import io
import logging
import re
from functools import partialmethod

import pytest
from rich.console import Console
from rich.live import Live

from chart_manager.cli import streams
from chart_manager.cli.progress import progress_view
from chart_manager.plumbing.logger import RichLogHandler
from chart_manager.plumbing.progress import (
    ProgressEvent,
    RowUpdate,
    failure,
    info,
    step,
    warn,
)


@pytest.fixture
def narrated(monkeypatch: pytest.MonkeyPatch) -> Console:
    """Swap the narration console (stderr) for a recording one."""
    console = Console(record=True, width=200)
    monkeypatch.setattr(streams, "narration", console)
    monkeypatch.setattr(streams, "errors", console)
    return console


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            step("Applying", "grafana:minimal -> observability"),
            "Applying grafana:minimal -> observability",
        ),
        (warn("could not list helm releases"), "warn: could not list helm releases"),
        (warn("cilium chart not found", label=None), "cilium chart not found"),
        (failure("failed", "GET [/api/v1/namespaces] 503"), "failed GET [/api/v1/namespaces] 503"),
        (info("pod/foo   0/1   [/nope]"), "pod/foo   0/1   [/nope]"),
        (
            RowUpdate(("grafana", "dev"), "schema", "failed", "2 errors"),
            "grafana/dev schema: failed 2 errors",
        ),
        (RowUpdate(("grafana", "dev"), "render", "passed", ""), "grafana/dev render: passed"),
    ],
)
def test_off_a_terminal_each_event_is_one_narration_line(
    narrated: Console, event: ProgressEvent | RowUpdate, expected: str
) -> None:
    with progress_view(live=False) as progress:
        progress(event)

    assert narrated.export_text() == expected + "\n"


def test_live_off_a_terminal_does_not_leave_a_table(narrated: Console) -> None:
    with progress_view(live=True) as progress:
        progress(RowUpdate(("grafana", "dev"), "render", "failed", ""))
        progress(step("Applying", "grafana"))
        progress(RowUpdate(("grafana", "dev"), "render", "passed", ""))
        progress(RowUpdate(("loki", "dev"), "schema", "failed", "boom"))

    assert narrated.export_text() == "Applying grafana\n"


def test_quiet_narration_silences_progress(narrated: Console) -> None:
    streams.set_narration_quiet(True)

    for live in (False, True):
        with progress_view(live=live) as progress:
            progress(step("Applying", "grafana"))
            progress(RowUpdate(("grafana", "dev"), "render", "passed", ""))

    assert narrated.export_text() == ""


@pytest.mark.parametrize("quiet", [False, True])
def test_live_clears_its_table_and_keeps_a_logged_warning_once(
    monkeypatch: pytest.MonkeyPatch, quiet: bool
) -> None:
    terminal = io.StringIO()
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr("sys.stderr", terminal)
    console = Console(stderr=True, force_terminal=True, color_system=None, width=200)
    monkeypatch.setattr(streams, "errors", console)
    monkeypatch.setattr(
        streams,
        "narration",
        Console(stderr=True, force_terminal=True, color_system=None, width=200, quiet=quiet),
    )
    # Draw synchronously rather than waiting for the background refresh thread.
    monkeypatch.setattr(Live, "update", partialmethod(Live.update, refresh=True))
    handler = RichLogHandler(console=streams.errors)

    with progress_view(live=True) as progress:
        progress(RowUpdate(("demo", "dev"), "render", "running", ""))
        handler.handle(
            logging.LogRecord(
                "test", logging.WARNING, __file__, 1, "provider unavailable", (), None
            )
        )

    output = terminal.getvalue()
    assert output.count("provider unavailable") == 1
    if quiet:
        assert "demo/dev" not in output
    else:
        assert "demo/dev" in output
        assert re.search(r"\x1b\[2K[^\n]*provider unavailable", output)
        # The final erase removes the live rows; redraw frames remain in the byte stream.
        assert output.endswith("\x1b[2K")
