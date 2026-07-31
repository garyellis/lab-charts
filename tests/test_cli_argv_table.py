"""Guard the argv indirection table in `tests/conftest.py`.

`conftest._COMMAND_PATHS` is the suite's single point of contact with the
CLI's command names. Its usefulness rests on two properties that nothing
else checks, because a table of strings is not type-checked and a stale
entry fails as a wall of unrelated red rather than as one clear message:

  (a) **Every right-hand side names a command the app really registers.**
      A rename that edits `cli/main.py` and mistypes the table -- `chrat`
      for `chart` -- would otherwise surface as "no such command" repeated
      across every migrated module, pointing at the call sites instead of
      at the one line that is wrong.

  (b) **Every command the app registers appears in the table**, as a key or
      as a value. This is the direction that keeps the seam from decaying:
      a new group added to `cli/main.py` with no entry here can be invoked
      by literal name from a test, and that literal is invisible until the
      day someone renames it. Keys count as coverage because a renamed
      command's old spelling stays a key forever while its new spelling
      becomes a value -- both are the table doing its job.

Only top-level names are covered. Subcommand entries (`("validate", "run")`)
appear when a command moves between groups; requiring one per command up
front would be a second, hand-maintained copy of the command tree.
"""

from __future__ import annotations

from typing import Any

import pytest
import typer.main

from .conftest import _COMMAND_PATHS, _root_app, resolve_argv


def _registered_paths() -> set[tuple[str, ...]]:
    """Every command path in the live app, as token tuples.

    Walked iteratively over `click.Group.commands` so this module never
    imports click, which is Typer's dependency rather than ours.
    """
    paths: set[tuple[str, ...]] = set()
    pending: list[tuple[Any, tuple[str, ...]]] = [(typer.main.get_command(_root_app()), ())]
    while pending:
        command, prefix = pending.pop()
        for name, child in getattr(command, "commands", {}).items():
            paths.add((*prefix, name))
            pending.append((child, (*prefix, name)))
    return paths


def test_the_app_has_a_command_tree_to_check() -> None:
    """Guard the guard: an empty walk makes both tests below vacuous."""
    paths = _registered_paths()
    assert len(paths) > 15, f"suspiciously small command tree: {sorted(paths)}"
    assert ("validate", "run") in paths
    assert ("upgrade-finalize",) in paths


def test_every_rewrite_target_resolves_against_the_app() -> None:
    """Property (a): the table may not point at a command that does not exist."""
    paths = _registered_paths()
    dangling = sorted(
        f"{' '.join(key)} -> {' '.join(value)}"
        for key, value in _COMMAND_PATHS.items()
        # A target may carry flags after the command path (`plan -o github`),
        # so only the leading non-option tokens name the command.
        if tuple(_command_tokens(value)) not in paths
    )
    assert not dangling, (
        "tests/conftest.py::_COMMAND_PATHS maps to commands the CLI does not "
        "register:\n  " + "\n  ".join(dangling)
    )


def test_every_registered_top_level_command_is_in_the_table() -> None:
    """Property (b): a new group must declare itself, or tests will hard-code it."""
    top_level = {path[0] for path in _registered_paths() if len(path) == 1}
    declared = {key[0] for key in _COMMAND_PATHS} | {
        value[0] for value in _COMMAND_PATHS.values()
    }
    missing = sorted(top_level - declared)
    assert not missing, (
        "these top-level commands have no entry in tests/conftest.py::_COMMAND_PATHS, "
        f"so tests will spell them literally and a rename will not be one edit: {missing}. "
        "Add an identity entry."
    )


def test_upgrade_finalize_is_never_rewritten() -> None:
    """`renovate-global.json`'s allowlist regex pins this literal invocation.

    Not a style rule: the regex is the security boundary Renovate uses to
    decide the command may run at all, and it matches the command name and
    flag order as text. See `tests/test_renovate_config_files.py`.
    """
    assert _COMMAND_PATHS[("upgrade-finalize",)] == ("upgrade-finalize",)
    assert resolve_argv(["upgrade-finalize", "--path", "charts/loki"]) == [
        "upgrade-finalize",
        "--path",
        "charts/loki",
    ]


@pytest.mark.parametrize(
    "leading",
    [
        ["--root", "/tmp/x"],
        ["--config=/tmp/c.yaml"],
        ["-vv", "-q"],
        ["--no-color", "-v", "--root", "/tmp/x"],
    ],
    ids=["value-option", "inline-value", "clustered-short-flags", "mixed"],
)
def test_global_options_before_the_command_path_are_preserved(leading: list[str]) -> None:
    """Click parses root options first, so translation must start after them.

    Asserted as "the options survive and the tail translates the same way it
    would on its own" rather than against a literal expected argv, so this
    test does not have to be edited every time an entry in the table moves.
    """
    command = ["charts", "list"]

    resolved = resolve_argv([*leading, *command])

    assert resolved[: len(leading)] == leading
    assert resolved[len(leading) :] == resolve_argv(command)


def test_an_unknown_leading_option_stops_the_scan() -> None:
    """`cli("-o", "json", "version")` asserts `-o` does *not* exist globally.

    If the scan skipped unknown options it would keep going, find `version`,
    and translate it -- so the test would assert against a command line the
    user could never type. Stopping at the unknown token means the argv
    reaches the app exactly as written, which is the whole assertion.
    """
    assert resolve_argv(["-o", "json", "version"]) == ["-o", "json", "version"]


def test_an_unknown_command_passes_through_untouched() -> None:
    """Tests assert that removed groups stay removed (`lifecycle`, `sandbox`)."""
    assert resolve_argv(["lifecycle", "--help"]) == ["lifecycle", "--help"]


def _command_tokens(argv: tuple[str, ...]) -> list[str]:
    """The leading tokens of a rewrite target that name a command."""
    tokens: list[str] = []
    for token in argv:
        if token.startswith("-"):
            break
        tokens.append(token)
    return tokens
