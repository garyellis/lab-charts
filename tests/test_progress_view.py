"""`progress_view`: every progress event reaches the narration console, as a line or a table."""

from __future__ import annotations

import pytest
from rich.console import Console

from chart_manager.cli import streams
from chart_manager.cli.progress import progress_view
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


def test_live_keeps_one_row_per_key_and_prints_lines_above_it(narrated: Console) -> None:
    with progress_view(live=True) as progress:
        progress(RowUpdate(("grafana", "dev"), "render", "running", ""))
        progress(step("Applying", "grafana"))
        progress(RowUpdate(("grafana", "dev"), "render", "passed", ""))
        progress(RowUpdate(("loki", "dev"), "schema", "failed", "boom"))

    text = narrated.export_text()
    assert text.index("Applying grafana") < text.index("render")
    assert text.count("grafana/dev") == 1
    assert "running" not in text
    assert "failed boom" in text


def test_quiet_narration_silences_progress(narrated: Console) -> None:
    streams.set_narration_quiet(True)

    for live in (False, True):
        with progress_view(live=live) as progress:
            progress(step("Applying", "grafana"))
            progress(RowUpdate(("grafana", "dev"), "render", "passed", ""))

    assert narrated.export_text() == ""
