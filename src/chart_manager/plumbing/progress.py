"""Progress: frozen events handed to one callback, no return value.

Long-running flows say what they are doing through these events and never decide how
they are shown. A `ProgressEvent` is a line: `severity` is its only rendering hint,
`label` carries its emphasis and `message` is left alone. A `RowUpdate` is the latest
status of one cell, keyed by what the row is about.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Severity = Literal["step", "detail", "warn", "error", "info"]


@dataclass(frozen=True)
class ProgressEvent:
    """One narration point emitted while a long-running flow runs."""

    severity: Severity
    message: str = ""
    label: str | None = None


@dataclass(frozen=True)
class RowUpdate:
    """The latest `status` of one `column` (a check, a phase) for the row `key` names."""

    key: tuple[str, ...]
    column: str
    status: str
    detail: str


Progress = Callable[[ProgressEvent | RowUpdate], None]


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


__all__ = [
    "Progress",
    "ProgressEvent",
    "RowUpdate",
    "Severity",
    "detail",
    "failure",
    "info",
    "step",
    "warn",
]
