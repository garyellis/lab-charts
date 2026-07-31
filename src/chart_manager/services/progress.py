"""Structured progress narration for the long-running cluster services.

`DevelopmentClusterService`, `EphemeralTestClusterService`, and cluster
bootstrap all take minutes to run and have to say what they are doing. None may
know *how* that narration is displayed -- the same run has to be renderable
by a Rich console, a Slack thread, or an SSE stream.

The contract is deliberately the same shape as the two callbacks that
already exist in this codebase (`ManifestValidationRunner(on_event=...)`,
`MonitorService(progress=...)`): one frozen event object, one callable, no
return value, exceptions from the callback are the surface's problem.

`severity` is the only rendering hint a surface gets. `label` is the short
prefix that carries the severity's emphasis ("Applying", "warn:"); the
surface styles the label and leaves `message` alone. A `label` of None
means the whole line carries the emphasis.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Severity = Literal["step", "detail", "warn", "error", "info"]


@dataclass(frozen=True)
class ProgressEvent:
    """One narration point emitted while a service runs."""

    severity: Severity
    message: str = ""
    label: str | None = None


ProgressCallback = Callable[[ProgressEvent], None]


def step(label: str, message: str = "") -> ProgressEvent:
    """A headline: the service is starting a named unit of work."""
    return ProgressEvent("step", message, label)


def detail(label: str, message: str = "") -> ProgressEvent:
    """A de-emphasized aside: a skip, a no-op, something the operator can ignore."""
    return ProgressEvent("detail", message, label)


def warn(message: str, *, label: str | None = "warn:") -> ProgressEvent:
    """A recoverable problem; the run continues. `label=None` emphasizes the whole line."""
    return ProgressEvent("warn", message, label)


def failure(label: str, message: str) -> ProgressEvent:
    """A failed unit of work. Whether the run aborts is the service's policy, not this event's."""
    return ProgressEvent("error", message, label)


def info(message: str) -> ProgressEvent:
    """Unstyled passthrough -- pre-formatted output such as kubectl diagnostics."""
    return ProgressEvent("info", message)


def emit(progress: ProgressCallback | None, event: ProgressEvent) -> None:
    """Deliver `event` if a callback is wired; no-op otherwise.

    A free function keeps optional progress reporting uniform across services.
    """
    if progress is not None:
        progress(event)


__all__ = [
    "ProgressCallback",
    "ProgressEvent",
    "Severity",
    "detail",
    "emit",
    "failure",
    "info",
    "step",
    "warn",
]
