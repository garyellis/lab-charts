"""Progress narration for cluster work: one frozen event, one callback, no return value.

Provision, bootstrap, install and test say what they are doing through these events and
never decide how they are shown. `severity` is the only rendering hint; `label` carries
its emphasis and `message` is left alone.
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
    """A headline: a named unit of work is starting."""
    return ProgressEvent("step", message, label)


def detail(label: str, message: str = "") -> ProgressEvent:
    """A de-emphasized aside: a skip, a no-op, something the operator can ignore."""
    return ProgressEvent("detail", message, label)


def warn(message: str, *, label: str | None = "warn:") -> ProgressEvent:
    """A recoverable problem; the run continues. `label=None` emphasizes the whole line."""
    return ProgressEvent("warn", message, label)


def failure(label: str, message: str) -> ProgressEvent:
    """A failed unit of work. Whether the run stops is the caller's decision."""
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
