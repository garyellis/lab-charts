"""Progress port for manifest validation.

The service layer defines the *shape* of progress narration; surfaces bring
the widgets. This module is the port — the `ProgressDisplay` protocol and
the no-op implementation a non-terminal surface uses by default. The Rich
adapters (live table, plain narration) live in `cli/validate_progress.py`,
the same split as `services/manifest_validation/wire.py` vs `cli/validate_render.py`.

Deliberately Rich-free: an HTTP worker driving `ManifestValidationService` has no
terminal and must not pull a TUI library into the process. A guard test in
`tests/test_manifest_validation_rendering.py` asserts it.

Why this port and not `services/progress.py`: that contract narrates a
linear sequence of prose events (`ProgressEvent(severity, message,
label)`) for the long-running cluster services. Validate's progress is a
*matrix* — `start` pre-sizes rows x phases, and each event addresses one
cell by `(row, phase)` so a live table can repaint it in place. Flattening
those coordinates into a message string discards exactly what the table
renders from, so the two stay separate siblings rather than one wrapping
the other.

Displays are stderr-only. Never touch stdout — JSON/markdown output must
remain pipeline-safe (`... -o json | jq ...`). Worker threads call
`on_event` concurrently, so implementations must be thread-safe.

Wiring example (what the CLI does):

    from chart_manager.cli.validate_progress import LiveTableDisplay
    from chart_manager.services.manifest_validation.app import RunRequest, ManifestValidationService

    request = RunRequest(skip_change_detection=True)
    ManifestValidationService(progress=LiveTableDisplay()).run(request)

`ManifestValidationService` owns the `start`/`stop` lifecycle and hands `on_event` to the
runner, so a surface only has to choose an implementation.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from chart_manager.services.manifest_validation.models import WorklistRow

__all__ = ["NullDisplay", "ProgressDisplay"]


@runtime_checkable
class ProgressDisplay(Protocol):
    """A sink for validate progress, addressed by (row, phase).

    Structural: an implementation need not subclass this (the in-repo ones
    do, for documentation). `start` receives the `WorklistRow`s that will
    be visited so the display can pre-size its UI; it takes rows rather
    than the runner's `RowConfig` so this module has no upward dependency
    on the runner package.
    """

    def start(self, rows: Sequence[WorklistRow]) -> None:
        """Initialize the display for the rows about to be visited."""
        ...

    def on_event(
        self,
        row: WorklistRow,
        phase: str,
        status: str,
        elapsed_s: float | None = None,
    ) -> None:
        """Handle a phase status change for one row (called from worker threads)."""
        ...

    def stop(self) -> None:
        """Tear down the display."""
        ...


class NullDisplay(ProgressDisplay):
    """No-op display: the default for machine output and non-terminal surfaces."""

    def start(self, _rows: Sequence[WorklistRow]) -> None:
        """No-op."""
        return

    def on_event(
        self,
        _row: WorklistRow,
        _phase: str,
        _status: str,
        _elapsed_s: float | None = None,
    ) -> None:
        """No-op."""
        return

    def stop(self) -> None:
        """No-op."""
        return


# A `ProgressFinalizer` used to wrap the display here, deduping terminal
# events a second time and re-emitting each finished row. It existed because
# the service drove N per-binding runners and had to stitch their event
# streams together. One runner now sees the whole worklist and already
# guarantees exactly one terminal event per (row, phase) -- including for
# phases nothing executed -- so a second dedupe over the same stream could
# only ever hide a runner-side regression.
