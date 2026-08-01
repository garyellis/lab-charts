"""The one place `cli/` decides what `--output` means.

Before this module the surface had three unrelated answers to "how do I ask
for machine-readable output?":

    cli/upgrade.py    --format text|json
    cli/validate.py   --format text|md|json|all
    cli/main.py       plan -o table|json|yaml|github
    cli/helmrelease.py --output pretty|json|auto     (the only correct one)

Four spellings of the same idea, two of them (`text`, `pretty`) different
words for one thing, and only `helmrelease` resolving `auto` from the
environment. This module collapses them onto one flag (`-o/--output`), one
vocabulary, and one resolver.

The vocabulary
--------------
`table`, `json`, `yaml`, `md` are the core. Two projections are local to a
single command and stay that way, because generalising them would be a lie:

  * `github` (`plan`) is a GitHub Actions matrix document. It is meaningless
    for `chart list`.
  * `all` (`chart validate`) is not a projection at all -- it is "text on
    stdout *plus* markdown and json sidecars written into the render dir".
    `.github/workflows/ci.yaml` depends on it.

A command therefore declares the subset it can actually produce, and asking
for one it cannot is a usage error rather than a silently different answer.
`md` in particular is offered only where a markdown projection exists.

`auto`, and why it asks about stdout
------------------------------------
The default is `auto`: `table` when stdout is a terminal and `CI` is not
`true`, else `json`. The probe is deliberately about *stdout* and not about
stderr or "is there a tty anywhere" -- the question `auto` answers is "is the
data I am about to emit going to a human or to a pipe", and that is a
property of the stream the projection lands on. Lifted from
`cli/helmrelease.py`, which was the only command that got this right.

`json` implies `--quiet`
------------------------
Design doc 6.2. A caller asking for JSON is feeding a parser, and progress
chatter interleaved on stderr is at best noise in their logs. Resolving to
`json` therefore silences narration process-wide via `cli/streams.py`.
Errors are *not* silenced -- see `streams.error_console`.

`--output` is a format; a file is `--to`
----------------------------------------
Design commitment 4, and the reason `grafana dashboard export` was renamed:
as `grafana export-dashboard`, its `-o` named the *destination file*, so
`chart-manager -o json grafana export-dashboard UID` wrote the dashboard
into a file called `json` -- silently, exiting 0. The command now takes
`--to PATH` and reads `-o` the way everything else does, and `_check` says
so by name when a rejected token looks like a path, because the old spelling
lives on in muscle memory and in scripts.

Why the global `-o` is not propagated through `default_map`
-----------------------------------------------------------
`cli/main.py` hands the global `--root` down with a nested Click
`default_map` keyed by parameter *name*. Doing the same for `output` would
seed every parameter that happens to be called `output`, whatever it means
there, and would defeat this module's precedence rule: `default_map` sits
below the command line but *above* the declared default, so a command's own
`-o` would arrive as the global value rather than as `None`, and "not given"
would become indistinguishable from "given globally".

So the global travels on `ctx.obj` instead, and only commands that opt in by
calling `resolve()` ever see it -- by construction rather than by an
exclusion list someone has to remember to update.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Annotated, Any

import typer
from rich.console import Console

from chart_manager.cli.streams import data_console, set_narration_quiet

#: Resolved from the environment rather than named by the caller.
AUTO = "auto"

TABLE = "table"
JSON = "json"
YAML = "yaml"
MD = "md"

#: Projections any command could reasonably offer.
CORE_MODES: tuple[str, ...] = (TABLE, JSON, YAML, MD)

#: `plan`-local: the GitHub Actions matrix document.
GITHUB = "github"
#: `chart validate`-local: text on stdout plus markdown/json sidecars.
ALL = "all"

#: Every token the surface recognises anywhere. The *global* `-o` is checked
#: against this at parse time so a typo fails immediately, while a token that
#: is real but unsupported by the command that ran gets the more specific
#: error from `resolve()`.
KNOWN_MODES: tuple[str, ...] = (*CORE_MODES, GITHUB, ALL)


#: Appended when the rejected token is a path rather than a typo'd format.
#: `grafana dashboard export` is the reason: its `-o` used to *be* the output
#: file, so the first thing an old script or an old habit produces here is a
#: path, and "unknown output: charts/x.json" on its own does not say where
#: the path was supposed to go.
_TO_HINT = " -- --output names a format; write to a file with --to"


def _looks_like_a_path(value: str) -> bool:
    """True for a token that was meant as a filename, not as a format.

    No format token contains a separator or an extension, so the three
    characters are sufficient and cannot misfire on a real projection name.
    """
    return bool(set(value) & {"/", "\\", "."})


def _check(value: str | None, allowed: Sequence[str], *, param_hint: str) -> str | None:
    """Reject an unknown token at parse time; `None` means "not given"."""
    if value is None:
        return None
    if value not in (*allowed, AUTO):
        raise typer.BadParameter(
            f"unknown output: {value} (allowed: {', '.join(allowed)})"
            + (_TO_HINT if _looks_like_a_path(value) else ""),
            param_hint=param_hint,
        )
    return value


def output_option(*allowed: str, extra_help: str = "") -> Any:
    """The `-o/--output` option metadata for a command that supports `allowed`.

    Returns the `typer.Option` rather than a finished `Annotated[...]` so the
    declaration at each call site stays a literal type alias::

        OutputOption = Annotated[str | None, output_option(TABLE, JSON)]

    A factory that returned the whole `Annotated[...]` would read better by
    one line and fail `mypy`: a module-level name bound to a *call result* is
    a variable, not a type alias, and "Variable is not valid as a type" is
    the error every use site then collects.

    Validation is attached as a Typer callback so it runs at *parse* time.
    That is not tidiness: `chart validate`'s format used to be checked only
    when the results were emitted, which is after the whole
    helm/kubeconform/kyverno pipeline had run -- so a typo cost a full
    validation run before saying so.

    Pair it with a `None` default, meaning "not given", which is distinct
    from `auto`: `None` is what lets a command fall back to the global `-o`,
    while an explicit `-o auto` still means "decide from the environment".
    """
    listed = ", ".join(allowed)
    return typer.Option(
        "--output",
        "-o",
        help=(
            f"Output projection: {listed}. "
            f"Default: auto ({TABLE} on a TTY, {JSON} otherwise).{extra_help}"
        ),
        callback=lambda value: _check(value, allowed, param_hint="--output"),
    )


#: The global `-o`, declared on the root callback. Accepts anything the
#: surface knows so a per-command projection can be named globally; whether
#: the command that runs supports it is `resolve()`'s call.
GlobalOutputOption = Annotated[
    str,
    typer.Option(
        "--output",
        "-o",
        help=(
            "Default output projection for this invocation: "
            f"{', '.join(KNOWN_MODES)}. A command's own --output still wins."
        ),
        callback=lambda value: _check(value, KNOWN_MODES, param_hint="--output"),
    ),
]


def global_output(ctx: typer.Context) -> str:
    """The invocation-wide `-o`, or `auto` when there is none.

    Read off `ctx.obj` by attribute rather than by importing
    `main.GlobalOptions`, which would be a cycle (`main` imports this
    module). `getattr` also covers the case a unit test builds a throwaway
    app with no root callback, where `ctx.obj` is None and `auto` is the
    right answer.
    """
    return getattr(ctx.obj, "output", None) or AUTO


def resolve(
    value: str | None,
    ctx: typer.Context,
    *,
    allowed: Sequence[str],
    console: Console | None = None,
) -> str:
    """Resolve the output mode for one command invocation.

    Precedence is `command -o` > global `-o` > `auto`, which is the same
    shape as the global `--root` and its per-command override.

    `console` is the stream the projection will land on; `auto` probes it for
    `is_terminal`. Callers that already hold their stdout console pass it so
    the decision and the writing cannot disagree.

    On `json` implying `--quiet` (design doc 6.2), note *`requested`*, not
    `selected`: narration is silenced only when the caller actually asked for
    json, never when `auto` merely resolved to it.

    That distinction is deliberate and is the one place this module departs
    from a literal reading of 6.2. `auto` resolves to json whenever stdout is
    not a terminal -- which includes every command in CI. Silencing on
    `selected` would therefore delete *all* operator narration from CI logs:
    `helmrelease promote`'s running commentary on a mutation (the thing
    `cli/helmrelease.py` explicitly keeps on stderr so `promote >/dev/null`
    still shows what happened), `chart validate`'s spec warnings, every
    "no dashboards found". The in-band-corruption problem 6.2 exists to
    prevent is already solved structurally by the stdout/stderr split
    (`cli/streams.py`), so silencing on top of it buys nothing and costs the
    diagnostics an operator reads when a CI job fails.

    Asking for json explicitly is a different statement: the caller named a
    machine format, so "data only" is a fair reading of their intent, and
    `-q` remains available to anyone who wants silence with a table.
    """
    requested = value if value is not None else global_output(ctx)
    selected = _auto(console) if requested == AUTO else requested
    if selected not in allowed:
        raise typer.BadParameter(
            f"this command has no '{selected}' projection (allowed: {', '.join(allowed)})",
            param_hint="--output",
        )
    set_narration_quiet(getattr(ctx.obj, "quiet", False) or requested == JSON)
    return selected


def require_dry_run(value: str | None, *, dry_run: bool) -> None:
    """Reject `-o` on a command whose only document is its `--dry-run` plan.

    `chart test` and `chart cache clean` emit no projection when they run
    for real -- one narrates progress, the other prints a status line. Their
    `-o` therefore names the form of the *plan*, and accepting it on a real
    run would be the accepted-and-ignored flag design doc 6.3 forbids: the
    caller asked for json, got a cluster install, and nothing said
    otherwise. Exit 2 naming the missing flag instead.

    Only an *explicit* per-command `-o` is rejected. The invocation-wide one
    is documented as a default that commands without a projection ignore, so
    `chart-manager -o json chart test x` must keep working rather than fail
    for having mentioned output at all.
    """
    if value is not None and not dry_run:
        raise typer.BadParameter(
            "this command's only document is its --dry-run plan; add --dry-run",
            param_hint="--output",
        )


def _auto(console: Console | None) -> str:
    """`table` for a human at a terminal, `json` for everything else."""
    if os.environ.get("CI") == "true":
        return JSON
    probe = console if console is not None else data_console()
    return TABLE if probe.is_terminal else JSON


__all__ = [
    "ALL",
    "AUTO",
    "CORE_MODES",
    "GITHUB",
    "JSON",
    "KNOWN_MODES",
    "MD",
    "TABLE",
    "YAML",
    "GlobalOutputOption",
    "global_output",
    "output_option",
    "require_dry_run",
    "resolve",
]
