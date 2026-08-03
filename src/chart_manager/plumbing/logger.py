"""Rich rendering for Python's standard logging package.

Call :func:`setup_logging` once at a process entrypoint. Application modules
then use ordinary ``logging.getLogger(__name__)`` loggers and remain unaware
of Rich or terminal rendering. Output goes to stderr so stdout stays suitable
for JSON and other machine-readable command results.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

from rich.console import Console
from rich.text import Text

#: Libraries that log per-request chatter at INFO, expecting consumers to
#: filter by namespace downstream; the root handler would render all of it.
#: Their WARNING and above still surface.
_NOISY_DEPENDENCY_LOGGERS = ("azure", "botocore", "urllib3")

_LEVEL_STYLES = {
    logging.DEBUG: "dim cyan",
    logging.INFO: "cyan",
    logging.WARNING: "yellow",
    logging.ERROR: "bold red",
    logging.CRITICAL: "bold white on red",
}


def _timestamp(record: logging.LogRecord) -> str:
    """Return the record creation time as an unambiguous UTC timestamp."""
    return datetime.fromtimestamp(record.created, tz=UTC).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _source(record: logging.LogRecord) -> str:
    """Identify the application call site that emitted a record."""
    return f"{record.name}.{record.funcName}:{record.lineno}"


class RichLogHandler(logging.Handler):
    """Render one markup-safe log record with time, severity, and call site."""

    def __init__(self, *, console: Console | None = None) -> None:
        super().__init__()
        self.console = console or Console(file=sys.stderr)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = Text()
            text.append(_timestamp(record), style="dim")
            text.append(" | ", style="dim")
            text.append(
                f"{record.levelname:<8}",
                style=_LEVEL_STYLES.get(record.levelno, "white"),
            )
            text.append(" | ", style="dim")
            text.append(_source(record), style="bold")
            text.append(" | ", style="dim")
            text.append(self.format(record))
            self.console.print(text)
        except Exception:
            self.handleError(record)


class JsonLogFormatter(logging.Formatter):
    """Render stable, machine-readable fields including the emitting call site."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["error"] = str(record.exc_info[1])
            entry["error_type"] = type(record.exc_info[1]).__name__
            entry["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(
    level: str = "INFO",
    *,
    fmt: str = "text",
    console: Console | None = None,
    stream: TextIO | None = None,
    force: bool = True,
) -> None:
    """Configure process-wide stderr logging in human-readable or JSON form."""
    normalized = level.upper()
    numeric_level = logging.getLevelNamesMapping().get(normalized)
    if not isinstance(numeric_level, int):
        raise ValueError(f"unknown log level: {level}")

    normalized_format = fmt.lower()
    if normalized_format == "text":
        handler: logging.Handler = RichLogHandler(console=console)
        handler.setFormatter(logging.Formatter("%(message)s"))
    elif normalized_format == "json":
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(JsonLogFormatter())
    else:
        raise ValueError(f"unknown log format: {fmt}")

    logging.basicConfig(
        level=numeric_level,
        handlers=[handler],
        force=force,
    )

    # At DEBUG the operator asked for wire-level detail, so dependencies
    # inherit it; set explicitly either way so repeated setup calls reset.
    dependency_level = (
        numeric_level if numeric_level <= logging.DEBUG else logging.WARNING
    )
    for name in _NOISY_DEPENDENCY_LOGGERS:
        logging.getLogger(name).setLevel(dependency_level)


__all__ = ["JsonLogFormatter", "RichLogHandler", "setup_logging"]
