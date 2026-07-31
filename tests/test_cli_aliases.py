"""The gate that makes the P1 rename wave reviewable.

P1 renames roughly twenty commands and keeps every old spelling working as
a hidden alias until P3. Twenty renames is too many to review by reading
them. This module replaces that reading with a property:

    An alias is indistinguishable from the command it replaces, except
    for one line on stderr saying so.

Four assertions, together, are what "indistinguishable" means here:

  (a) **hidden** -- the old name is absent from `--help`, so nothing new
      adopts a spelling that is being deleted.
  (b) **exit-code parity** -- a caller branching on `$?` sees no change.
      This is the one an alias built by re-implementing the command,
      instead of delegating to it, gets wrong first.
  (c) **exactly one deprecation line on stderr** -- not zero (a silent
      break on removal day), not two (noise that gets filtered wholesale,
      taking the real notice with it).
  (d) **stdout byte-identical** -- compared as bytes, not normalised and
      then compared. `.github/workflows/ci.yaml` captures CLI stdout into
      shell variables and several commands write JSON documents to it, so
      "same after stripping whitespace" is not the property those callers
      depend on. Byte equality also means the notice provably went to
      stderr; if it leaked onto stdout this assertion fails, which is why
      (c) and (d) are not redundant.

**The table starts empty.** No aliases exist yet. Whoever lands a rename
adds one line to `cli/main.py` and one line to `_CASES` below.

**Forgetting the second line is a failure, not a silent gap.**
`test_every_registered_alias_has_a_case` enumerates aliases from
`cli.deprecation.ALIASES` -- what the app actually registered -- and
requires the table to cover exactly that set. A gate that depends on
someone remembering to extend it is not a gate.

Why this module does not use `conftest.cli()`
---------------------------------------------
`conftest._COMMAND_PATHS` translates an old spelling into the new one, on
purpose, so the rest of the suite survives renames. Routing an alias test
through it would run the *new* command under both names and compare it
against itself -- the gate would pass vacuously on a completely broken
alias. Alias tests must reach the app with the exact tokens a user types,
so they use `CliRunner` directly. This is the one place in the suite where
that is correct.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import typer
import typer.main
from typer.testing import CliRunner, Result

from chart_manager.cli.deprecation import ALIASES, Alias, AliasRegistry, narrate
from chart_manager.cli.main import app as real_app

#: Deprecated command path -> argv that makes both spellings runnable.
#:
#: The value is appended to the old path and to the new one, so it holds
#: only the command's own arguments. `{root}` is replaced with an empty
#: tmp_path, which is what keeps most commands cluster- and network-free.
#:
#: Pick argv whose output is deterministic: (d) compares bytes, so a
#: duration, a timestamp or an unordered set rendered into stdout will
#: flake. A command with no such argv needs its own test, not an entry here.
#:
#: The emit commands are safe to run for real here: `tests/conftest.py`
#: pins `EVENTS_BACKEND=none` for the whole suite, so both spellings write to
#: `NullEventStore` -- no network, no clock in the output, and the `emitted
#: ...` confirmation is narration on stderr, leaving stdout empty on both
#: sides of the byte comparison.
_CASES: dict[tuple[str, ...], tuple[str, ...]] = {
    ("events", "build"): ("grafana@1.2.3", "--phase", "published"),
    # `--environment`, not `--env`: P1.3 owns that rename.
    ("events", "promote"): (
        "grafana@1.2.3",
        "--environment",
        "dev",
        "--phase",
        "promoted",
    ),
}


# --------------------------------------------------------------------------
# the assertions, factored so the self-test below exercises the same code
# --------------------------------------------------------------------------


def _command_at(app: typer.Typer, path: tuple[str, ...]) -> Any | None:
    """The Click command registered at `path`, or None if there is none."""
    node: Any = typer.main.get_command(app)
    for token in path:
        commands = getattr(node, "commands", None)
        if not commands or token not in commands:
            return None
        node = commands[token]
    return node


def _deprecation_lines(stderr: str) -> list[str]:
    """Every line of narration that announces a deprecation."""
    return [line for line in stderr.splitlines() if line.startswith("deprecated:")]


def _invoke(app: typer.Typer, argv: tuple[str, ...]) -> Result:
    """Run `argv` verbatim -- no `_COMMAND_PATHS` translation. See the docstring."""
    return CliRunner().invoke(app, list(argv))


def defects(app: typer.Typer, alias: Alias, argv: tuple[str, ...]) -> list[str]:
    """Every way `alias` differs from the command it replaces; empty means good.

    Returned rather than asserted so `test_the_gate_catches_a_broken_alias`
    can prove each clause fires, using this exact function.
    """
    found: list[str] = []

    command = _command_at(app, alias.old)
    if command is None:
        return [f"hidden: '{' '.join(alias.old)}' is not registered at all"]
    if not command.hidden:
        found.append(f"hidden: '{' '.join(alias.old)}' is listed in --help")

    old = _invoke(app, (*alias.old, *argv))
    new = _invoke(app, (*alias.new, *argv))

    if old.exit_code != new.exit_code:
        found.append(f"exit-code: alias exited {old.exit_code}, replacement exited {new.exit_code}")

    lines = _deprecation_lines(old.stderr)
    if len(lines) != 1:
        found.append(f"stderr: expected exactly one deprecation line, got {len(lines)}: {lines}")
    elif lines[0] != alias.message(_subcommand(alias, argv)):
        found.append(f"stderr: wrong wording: {lines[0]!r}")

    if _deprecation_lines(new.stderr):
        found.append("stderr: the replacement itself narrates a deprecation")

    if old.stdout_bytes != new.stdout_bytes:
        found.append(
            "stdout: not byte-identical\n"
            f"  alias:       {old.stdout_bytes!r}\n"
            f"  replacement: {new.stdout_bytes!r}"
        )

    return found


def _subcommand(alias: Alias, argv: tuple[str, ...]) -> str | None:
    """The subcommand a group alias reached, which its message must name."""
    if not alias.is_group:
        return None
    return next((token for token in argv if not token.startswith("-")), None)


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


def test_every_registered_alias_has_a_case() -> None:
    """The table is derived from the app, not from anyone's memory."""
    registered = {alias.old for alias in ALIASES}
    missing = sorted(" ".join(path) for path in registered - set(_CASES))
    stale = sorted(" ".join(path) for path in set(_CASES) - registered)

    assert not missing, (
        "these aliases are registered in cli/ but have no entry in _CASES, so "
        f"nothing checks that they behave like the command they replace: {missing}"
    )
    assert not stale, (
        "these _CASES entries name aliases the CLI no longer registers; delete "
        f"them (P3) or fix the path: {stale}"
    )


def test_registered_aliases_are_hidden() -> None:
    """(a), independent of `_CASES`, so it holds even before argv is chosen."""
    listed = sorted(
        " ".join(alias.old)
        for alias in ALIASES
        if (command := _command_at(real_app, alias.old)) is None or not command.hidden
    )
    assert not listed, f"aliases must not appear in --help: {listed}"


def test_upgrade_finalize_is_hidden_but_is_not_an_alias() -> None:
    """Guard the guard: prove `ALIASES` is a registry, not "every hidden command".

    `upgrade-finalize` is hidden and permanently frozen -- `renovate-global.json`
    pins its literal spelling in a security allowlist regex. If this gate had
    been written to discover aliases by scanning for `hidden=True` it would
    have adopted a command that must never be renamed or deprecated.
    """
    command = _command_at(real_app, ("upgrade-finalize",))
    assert command is not None and command.hidden
    assert ("upgrade-finalize",) not in {alias.old for alias in ALIASES}
    assert ("upgrade-finalize",) not in {alias.new for alias in ALIASES}


@pytest.mark.parametrize("old", sorted(_CASES), ids=lambda path: " ".join(path))
def test_alias_is_indistinguishable_from_its_replacement(
    old: tuple[str, ...], tmp_path: Path
) -> None:
    """(a) + (b) + (c) + (d) for one registered alias."""
    alias = next(candidate for candidate in ALIASES if candidate.old == old)
    argv = tuple(token.replace("{root}", str(tmp_path)) for token in _CASES[old])

    assert defects(real_app, alias, argv) == []


# --------------------------------------------------------------------------
# guard the guard: prove each clause of `defects` actually fires
# --------------------------------------------------------------------------
#
# `_CASES` is empty until P1 lands, so the parametrized test above generates
# no cases and asserts nothing. Without what follows, this whole module would
# be dead code that nobody notices is dead until the first rename ships
# behind it. These build a throwaway app with a deliberately broken alias per
# clause and assert `defects()` -- the same function the real gate calls --
# reports it.


def _fixture_app(defect: str | None) -> tuple[typer.Typer, Alias]:
    """A two-command app whose alias is broken in exactly one way."""
    registry = AliasRegistry()
    app = typer.Typer()
    group = typer.Typer()

    @group.command("run")
    def _run(name: str = typer.Option("world", "--name")) -> None:
        print(f"hello {name}")

    app.add_typer(group, name="new")

    if defect is None:
        return app, registry.group(app, group, old="old", new="new")

    alias = Alias(old=("old",), new=("new",), removed_in="0.6", is_group=True)
    hidden = defect != "hidden"

    def _callback(ctx: typer.Context) -> None:
        if defect == "twice":
            narrate(alias, ctx.invoked_subcommand)
            narrate(alias, ctx.invoked_subcommand)
        elif defect == "silent":
            pass
        else:
            narrate(alias, ctx.invoked_subcommand)
        if defect == "exit-code":
            raise typer.Exit(code=7)
        if defect == "stdout":
            print("extra chatter on the data channel")

    app.add_typer(group, name="old", hidden=hidden, callback=_callback)
    return app, alias


def test_a_correctly_registered_alias_reports_no_defects() -> None:
    """The negative control: without it, a `defects()` that always fires passes."""
    app, alias = _fixture_app(None)

    assert defects(app, alias, ("run", "--name", "there")) == []


@pytest.mark.parametrize(
    ("defect", "expected_clause"),
    [
        ("hidden", "hidden:"),
        ("exit-code", "exit-code:"),
        ("silent", "stderr:"),
        ("twice", "stderr:"),
        ("stdout", "stdout:"),
    ],
)
def test_the_gate_catches_a_broken_alias(defect: str, expected_clause: str) -> None:
    """Each clause of the contract, broken on purpose, must be reported."""
    app, alias = _fixture_app(defect)

    found = defects(app, alias, ("run", "--name", "there"))

    assert any(item.startswith(expected_clause) for item in found), (
        f"breaking '{defect}' produced no {expected_clause} finding: {found}"
    )


def test_the_deprecation_line_is_one_line_on_a_narrow_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rich wraps to the terminal width; the notice must not.

    A two-line notice is not a cosmetic problem: (c) counts lines, and an
    operator filtering `^deprecated:` would keep half the message.
    """
    monkeypatch.setenv("COLUMNS", "40")
    app, alias = _fixture_app(None)

    result = _invoke(app, ("old", "run"))

    assert _deprecation_lines(result.stderr) == [alias.message("run")]
