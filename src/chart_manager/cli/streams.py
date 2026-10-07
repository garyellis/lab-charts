"""The one place `cli/` decides which stream a console writes to.

The rule this module exists to enforce:

    The command's selected `--output` projection goes to stdout;
    everything else goes to stderr.

A human-readable table is the selected projection when `--output` is `table`,
so it goes to stdout too. `.github/workflows/ci.yaml` captures CLI stdout into
shell variables, so a stray warning on stdout corrupts the value.

These consoles pass `stderr=`, never `file=`: `Console(file=sys.stdout)` binds
the stream at construction, which bypasses `CliRunner` and
`redirect_stdout`. Ruff's TID251 bans importing `Console` outside the
modules allowlisted in `pyproject.toml`.
"""

from __future__ import annotations

import weakref

from rich.console import Console
from rich.markup import escape

from chart_manager.plumbing.progress import ProgressEvent

#: Every narration console handed out, so `set_narration_quiet` can reach them.
#: Weak, because `commands/promote/cli.py` builds one per call.
_QUIETABLE: weakref.WeakSet[Console] = weakref.WeakSet()

#: Applied to consoles built *after* a `set_narration_quiet` call.
_QUIET = False


def data_console(*, no_color: bool | None = None) -> Console:
    """Console for the selected `--output` projection. Writes to stdout.

    `no_color=None` (the default) leaves Rich's own detection in charge, so
    the `NO_COLOR` environment variable is still honored. Pass an explicit
    bool only when a `--no-color` flag should override that detection.

    Never quietable: `-q` and `--output json` suppress narration, not the answer.
    """
    return Console(stderr=False, no_color=no_color)


def narration_console(*, no_color: bool | None = None) -> Console:
    """Console for everything that is not the selected projection or an error.

    Writes to stderr, and is silenced by `set_narration_quiet`.
    """
    console = Console(stderr=True, no_color=no_color, quiet=_QUIET)
    _QUIETABLE.add(console)
    return console


def error_console(*, no_color: bool | None = None) -> Console:
    """Console for the reason a command failed. Writes to stderr, never silenced."""
    return Console(stderr=True, no_color=no_color)


def set_narration_quiet(quiet: bool) -> None:
    """Silence (or restore) every narration console, process-wide.

    Callers set it on every invocation, including `False`, so quiet never
    carries over from an earlier command.
    """
    global _QUIET
    _QUIET = quiet
    for console in _QUIETABLE:
        console.quiet = quiet


# --- the three shared consoles ----------------------------------------------

#: The selected `--output` projection -- tables, listings, JSON documents.
console = data_console()

#: Everything the caller did not ask for as output -- progress, hints, warnings.
narration = narration_console()

#: Terminal error reporting. Not silenced by `-q`, so a quiet run still says why it failed.
errors = error_console()


# --- progress ---------------------------------------------------------------

#: Severity -> Rich style for progress narration.
_PROGRESS_STYLES: dict[str, str | None] = {
    "step": "bold",
    "detail": "dim",
    "warn": "yellow",
    "error": "red",
    "info": None,
}


def print_progress(event: ProgressEvent) -> None:
    """Render one progress event to the narration console.

    The `label` carries the severity style; a label-less event styles the whole
    line. Both fields are escaped: they carry subprocess output, and an unmatched
    tag such as `[/etc/hosts]` raises Rich's MarkupError.
    """
    style = _PROGRESS_STYLES.get(event.severity)
    message = escape(event.message)
    label = None if event.label is None else escape(event.label)
    if label is None:
        text = f"[{style}]{message}[/{style}]" if style else message
    elif style:
        text = f"[{style}]{label}[/{style}] {message}".rstrip()
    else:
        text = f"{label} {message}".rstrip()
    narration.print(text)
