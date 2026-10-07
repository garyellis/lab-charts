"""The one place `cli/` decides what `--output` means.

`table`, `json`, `yaml`, `md` are the core projections. Two are local to one
command: `github` (`plan`, a GitHub Actions matrix) and `all` (`chart validate`,
text on stdout plus markdown and json sidecars, which `.github/workflows/ci.yaml`
depends on). Each command declares the subset it supports; any other is a
usage error.

`auto` is `table` when stdout is a terminal and not in CI, else `json`.
An explicit `json` also silences narration (see `resolve`). `--output` names a
format; commands that write a file take `--to`.

The global `-o` travels on `ctx.obj`, so only commands that call `resolve()`
see it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from chart_manager.cli import streams
from chart_manager.cli._container import container
from chart_manager.plumbing.yaml_files import dump_yaml

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

#: Every token the surface recognises anywhere; the global `-o` is checked
#: against this at parse time.
KNOWN_MODES: tuple[str, ...] = (*CORE_MODES, GITHUB, ALL)


#: Appended when the rejected token is a path rather than a typo'd format.
_TO_HINT = " -- --output names a format; write to a file with --to"


def _looks_like_a_path(value: str) -> bool:
    """True for a token that was meant as a filename, not as a format."""
    return bool(set(value) & {"/", "\\", "."})


def choice(
    value: str,
    allowed: Sequence[str],
    *,
    param_hint: str,
    noun: str = "value",
    accepted: Sequence[str] = (),
    hint: str = "",
) -> str:
    """Reject a token outside `allowed` as a usage error naming the flag.

    `accepted` is for a token that is legal but not advertised (`auto`, which
    every `--output` takes and no help text should enumerate as a projection).
    `hint` is appended verbatim -- see `_TO_HINT`.
    """
    if value in allowed or value in accepted:
        return value
    raise typer.BadParameter(
        f"unknown {noun}: {value} (allowed: {', '.join(allowed)}){hint}",
        param_hint=param_hint,
    )


def _check(value: str | None, allowed: Sequence[str], *, param_hint: str) -> str | None:
    """Reject an unknown `--output` token at parse time; `None` is "not given"."""
    if value is None:
        return None
    return choice(
        value,
        allowed,
        param_hint=param_hint,
        noun="output",
        accepted=(AUTO,),
        hint=_TO_HINT if _looks_like_a_path(value) else "",
    )


def output_option(*allowed: str, extra_help: str = "") -> Any:
    """The `-o/--output` option metadata for a command that supports `allowed`.

    Returns the `typer.Option` rather than a finished `Annotated[...]` so the
    declaration at each call site stays a literal type alias::

        OutputOption = Annotated[str | None, output_option(TABLE, JSON)]

    A type alias can't be the result of a function call, so mypy would
    reject a helper that built the whole `Annotated[...]`. Validation runs at
    parse time, before any slow work.

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


#: The global `-o`, declared on the root callback. Whether the command that
#: runs supports it is `resolve()`'s call.
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

    Read by attribute: importing `main.GlobalOptions` would be a cycle, and
    `ctx.obj` is None in an app without the root callback.
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

    Precedence is `command -o` > global `-o` > `auto`.

    `console` is the stream the projection will land on; `auto` probes it for
    `is_terminal`. Callers that already hold their stdout console pass it so
    the decision and the writing cannot disagree.

    Narration is silenced when json is *requested*, not when `auto` resolves
    to it, so CI logs (where stdout is never a terminal) keep their narration.
    """
    requested = value if value is not None else global_output(ctx)
    selected = _auto(console) if requested == AUTO else requested
    if selected not in allowed:
        raise typer.BadParameter(
            f"this command has no '{selected}' projection (allowed: {', '.join(allowed)})",
            param_hint="--output",
        )
    streams.set_narration_quiet(getattr(ctx.obj, "quiet", False) or requested == JSON)
    return selected


def require_dry_run(value: str | None, *, dry_run: bool) -> None:
    """Reject `-o` on a command whose only document is its `--dry-run` plan.

    Only a per-command `-o` is rejected; the global `-o` is a default that
    commands without a projection ignore.
    """
    if value is not None and not dry_run:
        raise typer.BadParameter(
            "this command's only document is its --dry-run plan; add --dry-run",
            param_hint="--output",
        )


def emit(data: Any, *, mode: str, table: Table | None = None) -> None:
    """Write one wire document in the resolved `--output` form.

    The caller builds the table; `table=None` is for callers that render their
    own terminal form and reach here only for json/yaml. Machine forms use
    `typer.echo` because Rich would wrap and highlight them.
    """
    if mode == JSON:
        typer.echo(json.dumps(data, indent=2, sort_keys=True))
    elif mode == YAML:
        typer.echo(dump_yaml(data), nl=False)
    elif table is None:
        raise ValueError(f"no table projection was supplied for --output {mode}")
    else:
        streams.console.print(table)


def _auto(console: Console | None) -> str:
    """`table` for a human at a terminal, `json` for everything else."""
    if container().settings.ci:
        return JSON
    probe = console if console is not None else streams.data_console()
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
    "choice",
    "emit",
    "global_output",
    "output_option",
    "require_dry_run",
    "resolve",
]
