"""Tests for ProgressDisplay implementations.

We inspect underlying state (cell maps, output buffers) rather than the
rendered Rich Live output — Live writes ANSI sequences to a real terminal
emulator, which is not what these tests should be coupled to.
"""
from __future__ import annotations

import io

from rich.console import Console

from chart_manager.plumbing.validate_models import WorklistRow
from chart_manager.services.validate.progress import (
    LiveTableDisplay,
    NullDisplay,
    PlainNarrationDisplay,
)


def _rows() -> list[WorklistRow]:
    return [
        WorklistRow(chart=c, env="dev", release=c, namespace="lab-dev")
        for c in ("alloy", "grafana")
    ]


def _row(chart: str) -> WorklistRow:
    return WorklistRow(chart=chart, env="dev", release=chart, namespace="lab-dev")


def test_null_display_is_a_noop() -> None:
    d = NullDisplay()
    d.start(_rows())
    d.on_event(_row("alloy"), "render", "PASS", 1.0)
    d.stop()  # must not raise


def test_plain_narration_emits_counter_and_elapsed() -> None:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=200)
    d = PlainNarrationDisplay(console=console)
    rows = _rows()
    d.start(rows)

    # running events are silent.
    d.on_event(_row("alloy"), "render", "running")
    # phase end events print a line each.
    d.on_event(_row("alloy"), "render", "PASS", 1.4)
    d.on_event(_row("alloy"), "schema", "PASS", 0.2)
    d.on_event(_row("alloy"), "policy", "SKIP", 0.0)
    d.on_event(_row("grafana"), "render", "PASS", 1.1)
    d.on_event(_row("grafana"), "schema", "PASS", 0.1)
    d.on_event(_row("grafana"), "policy", "PASS", 0.3)
    d.stop()

    output = buf.getvalue()
    # Counter bumps only on policy phase end.
    assert "[0/2] alloy/dev render…PASS (1.4s)" in output
    assert "[1/2] alloy/dev policy…SKIP" in output
    assert "[2/2] grafana/dev policy…PASS (0.3s)" in output


def test_live_table_cell_state_tracks_events() -> None:
    d = LiveTableDisplay(console=Console(file=io.StringIO()))
    rows = _rows()
    d.start(rows)
    try:
        d.on_event(_row("alloy"), "render", "running")
        d.on_event(_row("alloy"), "render", "PASS", 1.4)
        d.on_event(_row("grafana"), "render", "FAIL", 2.0)

        # _cells is internal but is the most direct way to assert update
        # correctness without scraping rendered ANSI.
        assert d._cells[("alloy", "dev")]["render"] == "PASS"
        assert d._cells[("grafana", "dev")]["render"] == "FAIL"
        assert d._cells[("alloy", "dev")]["schema"] == "…"
        # elapsed populated once a non-running event lands.
        assert d._cells[("alloy", "dev")]["elapsed"].endswith("s")
    finally:
        d.stop()


def test_live_table_ignores_unknown_rows() -> None:
    d = LiveTableDisplay(console=Console(file=io.StringIO()))
    d.start(_rows())
    try:
        # Off-list row — must not KeyError.
        d.on_event(_row("not-a-row"), "render", "PASS", 0.1)
    finally:
        d.stop()
