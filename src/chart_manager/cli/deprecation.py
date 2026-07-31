"""Register an old command name as a hidden alias of its replacement.

The contract, in prose, because the next twenty renames depend on it
holding and a reader needs to know *why* each clause is there:

  (1) **An alias is hidden.** It never appears in `--help`. `--help` is the
      documented surface; a deprecated spelling that advertises itself
      recruits new callers for a name that is being deleted.

  (2) **An alias emits exactly one line, on stderr, and nothing else.**
      Exactly one, because zero means a silent break at removal time and
      two means the noise gets filtered wholesale. On stderr, because the
      alias's *stdout is a data channel* -- `.github/workflows/ci.yaml`
      captures it into shell variables and `--format json` writes documents
      to it, so a deprecation notice on stdout would corrupt every aliased
      command's output in band. See `cli/streams.py`.

  (3) **An alias is otherwise byte-for-byte the command it replaces.** Not
      "equivalent" -- identical. That is what makes a twenty-command rename
      reviewable: the diff is a name, and `tests/test_cli_aliases.py`
      proves the behaviour did not travel with it. Both mechanisms below
      reach this by construction rather than by re-implementation:
      `AliasRegistry.group` registers the *same* Typer instance under the
      old name, and `AliasRegistry.command` registers a `functools.wraps`
      wrapper around the *same* function, so Click builds the same
      parameters and calls the same code.

  (4) **Every alias is discoverable.** Registration records the pair in an
      `AliasRegistry`, so `tests/test_cli_aliases.py` can enumerate what
      exists rather than trusting whoever added it to also remember the
      test. Hidden-ness alone is not a usable marker: `upgrade-finalize` is
      hidden and is not an alias, and never will be (`renovate-global.json`
      pins its literal spelling in a security allowlist).

Adding an alias is one line at the point of registration::

    ALIASES.group(app, chart_app, old="charts", new="chart")
    ALIASES.command(grafana_app, lint, old="grafana lint-dashboards",
                    new="grafana dashboard lint")

and one line in `tests/test_cli_aliases.py::_CASES` giving argv that makes
both spellings runnable. Omitting the second is a test failure, by design.

This module is deleted in P3 along with every alias it registered.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import typer
from rich.markup import escape

from chart_manager.cli.streams import narration_console

#: The release that deletes every alias, quoted in the deprecation line.
#:
#: One constant rather than a per-alias argument: the implementation plan
#: retires the whole set in a single phase (P3), so twenty different answers
#: to "when does this go away?" would all be the same answer maintained
#: twenty times. Derived from the plan's release boundaries -- P0 ships as
#: 0.2, P1 as 0.3, P2.1 alone as 0.4, P2.2/P2.3 as 0.5, P3 as 0.6. Move this
#: one line if the schedule moves.
REMOVED_IN = "0.6"

#: Narration sink. Built through `cli/streams.py` so the stdout/stderr split
#: has exactly one owner; see `tests/test_output_streams.py`.
_NARRATION = narration_console()


@dataclass(frozen=True)
class Alias:
    """One deprecated command path and the path that replaced it.

    Paths are full, space-separated, root-relative invocations as a user
    types them (`("charts", "test")`), not the local name the command is
    registered under. The message names what the user typed and what they
    should type instead; a local name would name neither.

    `new` may carry flags (`("plan", "-o", "github")`) for a command whose
    replacement is a projection of a more general one. The leading
    non-option tokens are the command path; the rest is advice.
    """

    old: tuple[str, ...]
    new: tuple[str, ...]
    removed_in: str
    #: True when the whole group is aliased, so the message must name the
    #: subcommand the user actually reached.
    is_group: bool = False

    def message(self, subcommand: str | None = None) -> str:
        """The single stderr line, per the design doc's stated format."""
        old = " ".join((*self.old, *((subcommand,) if subcommand else ())))
        new = " ".join((*self.new, *((subcommand,) if subcommand else ())))
        return f"deprecated: '{old}' -> '{new}' (removed in {self.removed_in})"


def narrate(alias: Alias, subcommand: str | None = None) -> None:
    """Write the one deprecation line to stderr.

    `soft_wrap` is not cosmetic: Rich wraps to the terminal width, and
    `deprecated: 'ci cluster-test-matrix' -> 'plan -o github' (removed in
    0.6)` is 73 characters. On an 80-column terminal that is one line; on a
    narrower one it silently becomes two, and "exactly one line" -- the
    property `tests/test_cli_aliases.py` asserts and the property that makes
    the notice filterable -- would hold only on the maintainer's laptop.
    """
    _NARRATION.print(escape(alias.message(subcommand)), soft_wrap=True)


class AliasRegistry:
    """The set of aliases the surface has registered, in registration order.

    A class rather than a module-level list so a test can build an isolated
    registry against a throwaway app and exercise this module without
    polluting what `tests/test_cli_aliases.py` enumerates from the real CLI.
    `ALIASES` below is the one the real surface uses.
    """

    def __init__(self) -> None:
        self._aliases: list[Alias] = []

    def __iter__(self) -> Iterator[Alias]:
        return iter(self._aliases)

    def __len__(self) -> int:
        return len(self._aliases)

    def group(
        self,
        parent: typer.Typer,
        group: typer.Typer,
        *,
        old: str,
        new: str,
        removed_in: str = REMOVED_IN,
    ) -> Alias:
        """Mount `group` a second time under its old name, hidden.

        The *same* Typer instance is mounted twice, so every subcommand
        under the old name is the identical command object reached under the
        new one -- there is no second copy to drift. The alias's group
        callback narrates and names `ctx.invoked_subcommand`, which is what
        turns one registration into a correct message for each of the
        group's commands.

        Raises if `group` declares its own callback: Typer's `add_typer`
        takes `callback` as an override, not an addition, so aliasing such a
        group would silently drop its group-level options under the old name
        and break the byte-identical property this module exists to
        guarantee. Compose the two callbacks explicitly if that day comes.
        """
        if group.registered_callback is not None:
            raise ValueError(
                f"cannot alias '{new}' as '{old}': the group declares its own callback, "
                "which Typer's add_typer(callback=...) would replace rather than wrap. "
                "Compose the deprecation notice into that callback instead."
            )
        alias = Alias(old=_path(old), new=_path(new), removed_in=removed_in, is_group=True)

        def _narrate(ctx: typer.Context) -> None:
            narrate(alias, ctx.invoked_subcommand)

        parent.add_typer(group, name=alias.old[-1], hidden=True, callback=_narrate)
        self._aliases.append(alias)
        return alias

    def command(
        self,
        parent: typer.Typer,
        target: Callable[..., Any],
        *,
        old: str,
        new: str,
        removed_in: str = REMOVED_IN,
        bind: Mapping[str, Any] | None = None,
    ) -> Alias:
        """Register `target` a second time under its old name, hidden.

        `functools.wraps` is load-bearing rather than tidy: Typer derives a
        command's parameters from `inspect.signature`, which follows
        `__wrapped__`, so the alias declares byte-identical flags, defaults
        and help text without restating any of them. The wrapper narrates
        and then calls the same function, so exit codes and stdout are the
        target's own.

        `bind` freezes named parameters of `target` to fixed values. It
        exists because several P1 renames do not map an old name onto a new
        name but onto **a new command plus flags** -- `ci cluster-test-matrix`
        became `plan -o github`, one projection of a general command. Without
        it those three renames would each have to re-implement the command
        body under the old name, which is precisely the "alias built by
        re-implementing" that clause (3) and `tests/test_cli_aliases.py`
        exist to prevent.

        A bound parameter is *removed from the alias's signature*, not merely
        overridden. Two reasons, both correctness rather than taste:

          * Leaving `-o` on `ci cluster-test-matrix` while ignoring whatever
            the user passed is a silent wrong answer -- `-o json` would
            still emit the GitHub matrix. Removing it makes that argv the
            same hard parse error it is today.
          * The old command genuinely had no such flag. "Byte-identical to
            the command it replaces" is the property this module sells;
            advertising a flag the old spelling never had breaks it in the
            other direction.

        Typer reads `inspect.signature`, which honours an explicit
        `__signature__` over the `__wrapped__` chain, so one assignment is
        enough to make Click build the narrower parameter list.

        `eval_str=True` is not optional. Every CLI module runs under
        `from __future__ import annotations`, so an unevaluated signature
        carries annotations as *strings*: Typer then cannot see the
        `typer.Option("--all", ...)` inside `Annotated[...]` and falls back to
        deriving a flag name from the parameter name, silently turning
        `--all` into `--all-charts` and `--chart` into `--charts`. That is a
        broken alias that still parses, which is the worst failure mode this
        module has.
        """
        alias = Alias(old=_path(old), new=_path(new), removed_in=removed_in)
        bound = dict(bind or {})

        @functools.wraps(target)
        def aliased(*args: Any, **kwargs: Any) -> Any:
            narrate(alias)
            return target(*args, **{**kwargs, **bound})

        if bound:
            signature = inspect.signature(target, eval_str=True)
            unknown = sorted(set(bound) - set(signature.parameters))
            if unknown:
                raise ValueError(
                    f"cannot alias '{new}' as '{old}': bind names parameters that "
                    f"{getattr(target, '__name__', target)!r} does not declare: {unknown}"
                )
            aliased.__signature__ = signature.replace(  # type: ignore[attr-defined]
                parameters=[
                    parameter
                    for name, parameter in signature.parameters.items()
                    if name not in bound
                ]
            )

        parent.command(alias.old[-1], hidden=True)(aliased)
        self._aliases.append(alias)
        return alias


def _path(spec: str) -> tuple[str, ...]:
    """Split a space-separated command path, rejecting an empty one."""
    parts = tuple(spec.split())
    if not parts:
        raise ValueError("a command path may not be empty")
    return parts


#: The registry the real CLI registers into, and the one the alias gate
#: enumerates. Empty until P1 lands its first rename.
ALIASES = AliasRegistry()


__all__ = ["ALIASES", "REMOVED_IN", "Alias", "AliasRegistry", "narrate"]
