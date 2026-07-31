"""The one place `cli/` decides which stream a console writes to.

The rule this module exists to enforce:

    The command's selected `--output` projection goes to stdout;
    everything else goes to stderr.

Note this is more precise than "stdout is data, stderr is narration". A
human-readable table *is* the selected projection when `--output` resolves
to text, so it belongs on stdout -- otherwise `chart-manager charts list |
less` shows an empty page. What goes to stderr is everything the user did
not ask for as output: progress, warnings, hints, deprecation notices, and
error detail.

Getting this wrong is not cosmetic. `.github/workflows/ci.yaml` captures
CLI stdout into shell variables, and `cli/validate.py --format json` writes
a JSON document to stdout; a single stray warning on the same stream
corrupts the value in band, where no exit code reveals it.

Why these consoles pass `stderr=` and never `file=`
---------------------------------------------------
`Console(file=sys.stdout)` resolves the stream **at construction time**.
A module-level console built that way captures the interpreter's real
stdout at import, so anything that later replaces `sys.stdout` -- Click's
`CliRunner`, `contextlib.redirect_stdout`, a future embedding surface --
is silently bypassed and the output vanishes. `Console(stderr=False|True)`
resolves `sys.stdout`/`sys.stderr` lazily on every write, which is what a
process-wide seam needs. `tests/test_output_streams.py` enforces that every
`Console(...)` under `cli/` names its stream explicitly one way or the other,
so a bare `Console()` -- which silently means stdout -- cannot come back.
"""

from __future__ import annotations

from rich.console import Console


def data_console(*, no_color: bool | None = None) -> Console:
    """Console for the selected `--output` projection. Writes to stdout.

    `no_color=None` (the default) leaves Rich's own detection in charge, so
    the `NO_COLOR` environment variable is still honored. Pass an explicit
    bool only when a `--no-color` flag should override that detection.
    """
    return Console(stderr=False, no_color=no_color)


def narration_console(*, no_color: bool | None = None) -> Console:
    """Console for everything that is not the selected projection. Writes to stderr."""
    return Console(stderr=True, no_color=no_color)


#: Shared narration sink for the module-level `narrate()` helper below.
_NARRATION = narration_console()


def narrate(message: str) -> None:
    """Print one line of narration to stderr.

    For call sites that just need a warning or a status line and have no
    reason to hold a console of their own.
    """
    _NARRATION.print(message)
