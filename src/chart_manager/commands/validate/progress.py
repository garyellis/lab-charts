"""How `validate.run()` reports each check as it starts and finishes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from chart_manager.commands.validate.models import Row


class Progress(Protocol):
    """A sink for check events, addressed by (row, check); called from worker threads."""

    def start(self, rows: Sequence[Row]) -> None:
        """Prepare to show these rows."""

    def on_event(self, row: Row, check: str, status: str, elapsed_s: float | None = None) -> None:
        """A check on `row` is `running`, or finished with `status`."""

    def stop(self) -> None:
        """The run is over."""


class _Silent:
    def start(self, rows: Sequence[Row]) -> None:
        pass

    def on_event(self, row: Row, check: str, status: str, elapsed_s: float | None = None) -> None:
        pass

    def stop(self) -> None:
        pass


#: Reports nothing: for machine output and callers without a terminal.
NULL_PROGRESS: Progress = _Silent()
