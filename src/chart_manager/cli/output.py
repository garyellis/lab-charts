"""The one place `cli/` decides what `--output` means.

`table`, `json`, `yaml`, `md` are the core projections. Two are local to one
command: `github` (`plan`, a GitHub Actions matrix) and `all` (`chart validate`,
text on stdout plus markdown and json sidecars, which `.github/workflows/ci.yaml`
depends on). Each command declares the subset it supports; any other is a
usage error.

`auto` is `table` when stdout is a terminal and not in CI, else `json`.
An explicit `json` also silences narration (see `resolve`). `--output` names a
format; commands that write a file take `--to`.

`finish()` writes a result's document and exits with its outcome's code;
`emit()` writes a document with no outcome, such as a dry-run plan.

The global `-o` travels on `ctx.obj`, so only commands that call `resolve()`
see it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from typing import Annotated, Any, NoReturn, Protocol

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from chart_manager.cli import streams
from chart_manager.cli._container import container
from chart_manager.plumbing.documents import to_document
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
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
    console: Console,
) -> str:
    """Resolve the output mode for one command invocation.

    Precedence is `command -o` > global `-o` > `auto`.

    `console` is the stream the projection will land on; `auto` probes it for
    `is_terminal`, so the decision and the writing cannot disagree.

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


def to_json(document: Any) -> str:
    """The one json form of a document: indented, keys sorted."""
    return json.dumps(document, indent=2, sort_keys=True)


def emit(data: Any, *, mode: str, table: Table | None = None) -> None:
    """Write a document that has no outcome in the resolved `--output` form.

    The caller builds the table; `table=None` is for callers that render their
    own terminal form and reach here only for json/yaml. Machine forms use
    `typer.echo` because Rich would wrap and highlight them.
    """
    if mode == JSON:
        typer.echo(to_json(data))
    elif mode == YAML:
        typer.echo(dump_yaml(data), nl=False)
    elif table is None:
        raise ValueError(f"no table projection was supplied for --output {mode}")
    else:
        streams.console.print(table)


class HasOutcome(Protocol):
    """A command result that knows how its run ended."""

    @property
    def outcome(self) -> Outcome: ...


def finish[R: HasOutcome](result: R, *, mode: str, render: Callable[[R], None]) -> NoReturn:
    """Write `result` in `mode`, then exit with the code for `result.outcome`.

    json and yaml write the result's document; any other mode calls `render`,
    which prints the human form and any narration that follows it.
    """
    if mode == JSON:
        typer.echo(to_json(to_document(result)))
    elif mode == YAML:
        typer.echo(dump_yaml(to_document(result)), nl=False)
    else:
        render(result)
    raise typer.Exit(code=exit_code_for(result.outcome))


def document_table(document: Any, *, title: str) -> Table:
    """Render a document as a Field/Value table over dotted paths.

    Flattening the document, rather than a hand-written layout, means a field
    added to the document shows up in the table without editing a renderer.
    """
    table = Table("Field", "Value", title=title)
    for field, value in _flatten(document, ""):
        table.add_row(escape(field), escape(value))
    return table


def _flatten(value: Any, prefix: str) -> Iterator[tuple[str, str]]:
    """Walk a document into (dotted path, rendered leaf) rows.

    A list of scalars stays on one row (`values: a.yaml, b.yaml`); a list of
    objects is indexed, because its members have structure worth addressing.
    """
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from _flatten(item, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, list) and value:
        if any(isinstance(item, dict | list) for item in value):
            for index, item in enumerate(value):
                yield from _flatten(item, f"{prefix}[{index}]")
        else:
            yield prefix, ", ".join(_leaf(item) for item in value)
    else:
        yield prefix, _leaf(value)


def _leaf(value: Any) -> str:
    """One leaf as JSON spells it (`true`, `null`), minus the quotes around strings."""
    return value if isinstance(value, str) else json.dumps(value)


def _auto(console: Console) -> str:
    """`table` for a human at a terminal, `json` for everything else."""
    if container().settings.ci:
        return JSON
    return TABLE if console.is_terminal else JSON


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
    "HasOutcome",
    "choice",
    "document_table",
    "emit",
    "finish",
    "global_output",
    "output_option",
    "require_dry_run",
    "resolve",
    "to_json",
]
