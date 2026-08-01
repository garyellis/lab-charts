"""The one place `cli/` decides which stream a console writes to.

The rule this module exists to enforce:

    The command's selected `--output` projection goes to stdout;
    everything else goes to stderr.

Note this is more precise than "stdout is data, stderr is narration". A
human-readable table *is* the selected projection when `--output` resolves
to text, so it belongs on stdout -- otherwise `chart-manager chart list |
less` shows an empty page. What goes to stderr is everything the user did
not ask for as output: progress, warnings, hints, deprecation notices, and
error detail.

Getting this wrong is not cosmetic. `.github/workflows/ci.yaml` captures
CLI stdout into shell variables, and `cli/validate.py -o json` writes
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

import weakref

from rich.console import Console
from rich.markup import escape

from chart_manager.services.progress import ProgressEvent

#: Every narration console handed out, so `set_narration_quiet` can reach the
#: ones built at import time here as well as the ones `helmrelease.py` builds
#: per invocation.
#:
#: A `WeakSet` rather than a list because `helmrelease.py` builds a console per
#: call: in a process-per-invocation CLI a list would be equivalent, but this
#: module is the process-wide seam a long-lived surface would also use, and
#: there a list is an unbounded leak.
_QUIETABLE: weakref.WeakSet[Console] = weakref.WeakSet()

#: Applied to consoles built *after* a `set_narration_quiet` call. Needed
#: because `--output json` is resolved inside a command, which is after this
#: module built its three shared consoles but before `helmrelease.py` builds
#: its per-call ones.
_QUIET = False


def data_console(*, no_color: bool | None = None) -> Console:
    """Console for the selected `--output` projection. Writes to stdout.

    `no_color=None` (the default) leaves Rich's own detection in charge, so
    the `NO_COLOR` environment variable is still honored. Pass an explicit
    bool only when a `--no-color` flag should override that detection.

    Never registered as quietable: `-q` and `--output json` suppress the
    narration *around* the answer, never the answer itself.
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
    """Console for the reason a command failed. Writes to stderr, never silenced.

    Split from `narration_console` because the two differ in exactly one
    respect and it is the one that matters: `-q` (and `--output json`, which
    implies it -- design doc 6.2) suppress narration so a pipeline sees only
    the projection. Suppressing *errors* along with it would make `-q`
    indistinguishable from `2>/dev/null`, and a failing command would exit
    nonzero having said nothing about why.
    """
    return Console(stderr=True, no_color=no_color)


def set_narration_quiet(quiet: bool) -> None:
    """Silence (or restore) every narration console, process-wide.

    Two callers, both in `cli/`:

      * `main.global_options`, for `-q` alone. It runs for every invocation,
        including commands that have no `--output` at all (`local up`,
        `local down`), which is why `-q` cannot be left to the resolver.
      * `cli/output.resolve`, which folds `-q` together with an *explicitly*
        requested `-o json` (design doc 6.2).

    Both write on every invocation, including the `False` case, so the state
    stays derived from the current command rather than accumulating across
    commands -- see the note in `output.resolve` on why an only-ever-True
    version was sticky and wrong.

    Consoles already handed out are updated in place *and* the flag is
    remembered for consoles built later, because those two sets are both
    non-empty at the moment this is called.
    """
    global _QUIET
    _QUIET = quiet
    for console in _QUIETABLE:
        console.quiet = quiet


# --- the three shared consoles ----------------------------------------------
#
# One set for the whole surface. Every `cli/` module used to derive its own --
# `main.py` called it `console`, `publish.py` called the same thing `data`,
# `doctor.py` built a fresh pair inside the command body -- so `--no-color`
# reached exactly the three `main.py` happened to hold and nothing else, and
# "which console does this line go to?" was answered per module. Import these
# instead; `from ... import console` still binds a module-level name, so a
# test that swaps one module's console keeps working.

#: The selected `--output` projection -- tables, listings, JSON documents.
#: Goes to stdout, because that is what a caller pipes or captures.
console = data_console()

#: Everything the caller did not ask for as output -- progress, hints,
#: warnings. Goes to stderr so it can never corrupt `console`.
narration = narration_console()

#: Terminal error reporting. Same stream as `narration`, separate console
#: because `-q` silences narration and must not silence the reason a command
#: failed -- a quiet run that dies with no output is unsupportable.
#: `error_console()` rather than `narration_console()` is what makes that
#: structural: only narration consoles are registered with
#: `set_narration_quiet`, which `-q` and `--output json` both drive.
errors = error_console()


def narrate(message: str) -> None:
    """Print one line of narration to stderr.

    For call sites that just need a warning or a status line and have no
    reason to hold a console of their own.
    """
    narration.print(message)


# --- service progress -------------------------------------------------------

#: Severity -> Rich style for the narration the long-running services emit.
#: The service picks the severity; only this table knows it becomes markup.
_PROGRESS_STYLES: dict[str, str | None] = {
    "step": "bold",
    "detail": "dim",
    "warn": "yellow",
    "error": "red",
    "info": None,
}


def print_progress(event: ProgressEvent) -> None:
    """Render one service progress event to the narration console.

    Here rather than in a command module because every long-running service
    on this surface -- converge, ephemeral test, cluster lifecycle -- hands
    its `progress=` callback the same shape, and the severity-to-style table
    is the whole of the decision.

    The event's `label` carries the severity emphasis and `message` stays
    plain, which reproduces the `[bold]Applying[/bold] chart:profile` shape
    the services used to build themselves. A label-less event emphasizes
    the whole line.

    Both fields are escaped before they reach Rich. They carry subprocess
    output -- helm/kubectl stderr, raw `kubectl get events` dumps -- and an
    unmatched closing tag (a bracketed path like `[/etc/hosts]`, a JSON
    Patch path, an XML fragment) raises MarkupError. That turned the one
    diagnostic an operator needs into a traceback.
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
