"""The exit-code table is the process contract; pin it as data.

`plumbing/exit_codes.py` is the only module allowed to say what number an
outcome is worth, which makes it the only place a silent renumbering could
happen. These tests are deliberately literal: an expectation derived from
the table under test would pass no matter what the table said.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from chart_manager.plumbing.errors import (
    CapabilityUnavailableError,
    ChartManagerError,
    ChartNotFoundError,
    CommandTimeout,
    DependencyCycleError,
    ExternalCommandError,
    MissingToolError,
    SpecError,
)
from chart_manager.plumbing.exit_codes import (
    EXIT_CODE,
    EXIT_ENVIRONMENT,
    EXIT_FAILED,
    EXIT_MISSING_BINARY,
    EXIT_SPEC,
    EXIT_SUCCESS,
    EXIT_TOOL,
    EXIT_USAGE,
    Outcome,
    exit_code_for,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "chart_manager"


def test_table_is_exhaustive_over_every_outcome() -> None:
    """A new `Outcome` must not be addable without choosing its code.

    Without this, `exit_code_for` would raise KeyError at runtime -- or,
    "fixed" with a `.get(outcome, 0)` default, would report a brand-new
    failure mode as success.
    """
    assert set(EXIT_CODE) == set(Outcome)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (Outcome.SUCCESS, 0),
        (Outcome.FAILED, 1),
        (Outcome.USAGE, 2),
        (Outcome.SPEC, 3),
        (Outcome.TOOL, 4),
        (Outcome.ENVIRONMENT, 5),
        (Outcome.MISSING_BINARY, 127),
    ],
)
def test_each_outcome_maps_to_the_code_design_6_1_assigns_it(
    outcome: Outcome,
    expected: int,
) -> None:
    """Design §6.1's table, transcribed. Changing a row is a release event."""
    assert exit_code_for(outcome) == expected


def test_named_constants_agree_with_the_table() -> None:
    """The constants exist so no caller writes a literal; keep them honest."""
    assert (EXIT_SUCCESS, EXIT_FAILED, EXIT_USAGE, EXIT_SPEC) == (0, 1, 2, 3)
    assert (EXIT_TOOL, EXIT_ENVIRONMENT, EXIT_MISSING_BINARY) == (4, 5, 127)


def test_success_is_zero() -> None:
    """The hinge between the wire `ok` field and `$?`.

    `services/helmrelease/wire.py` publishes `ok = outcome is SUCCESS` while
    `cli/helmrelease.py` exits `exit_code_for(outcome)`. Those two agree only
    because SUCCESS is 0 and nothing else is. Asserted here rather than left
    implicit, because the coupling is otherwise invisible from either side.
    """
    assert exit_code_for(Outcome.SUCCESS) == 0
    assert [o for o in Outcome if exit_code_for(o) == 0] == [Outcome.SUCCESS]


def test_plumbing_exit_codes_does_not_import_a_service() -> None:
    """Keep the table keyed on `Outcome`, not on some vertical's status enum.

    This is the constraint that produced the current shape: a
    `Mapping[PromoteStatus, int]` here would make `plumbing/` depend on
    `services/`, which nothing in `plumbing/` does today. See the module
    docstring, and `test_layering.py::test_plumbing_does_not_import_service_domains`
    for the general rule this specialises.
    """
    path = _SRC / "plumbing" / "exit_codes.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)

    offenders = [m for m in imported if m.startswith("chart_manager.services")]
    assert not offenders, f"plumbing/exit_codes.py imports services: {offenders}"


# --------------------------------------------------------------------------
# the surface speaks Outcome, never a number
# --------------------------------------------------------------------------
#
# Static, in the spirit of `test_layering.py` and `test_output_streams.py`:
# the behavioral tests below can only cover the exit sites they enumerate,
# and the failure mode being guarded is a *new* command written by someone
# who never read this module and reached for `typer.Exit(1)` because it is
# shorter. That is how the table ended up with one consumer the first time.

#: `raise typer.Exit(...)` and `sys.exit(...)` -- the two ways a Python CLI
#: sets `$?`. Both are scanned, because `cli/validate.py` legitimately uses
#: the second one and a gate that only knew the first would wave it through.
_EXIT_CALLS = frozenset({"Exit", "exit"})

#: The one module allowed to write exit-code integers.
_TABLE = Path("chart_manager") / "plumbing" / "exit_codes.py"


def _exit_call_sites() -> list[tuple[Path, ast.Call]]:
    """Every `typer.Exit(...)` / `sys.exit(...)` under `src/`, as (path, node)."""
    found: list[tuple[Path, ast.Call]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in _EXIT_CALLS:
                found.append((path, node))
    return found


def _literal_code(node: ast.Call) -> int | None:
    """The integer literal this exit call was given, if it was given one.

    Covers both spellings -- `typer.Exit(code=1)` and `sys.exit(1)` -- and
    returns None for anything computed, which is what a call routed through
    `exit_code_for` looks like.
    """
    args = [*node.args, *(kw.value for kw in node.keywords if kw.arg in (None, "code"))]
    for arg in args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
            return arg.value
    return None


def test_exit_scan_finds_the_call_sites_it_is_meant_to_check() -> None:
    """Guard the guard: an empty sweep would make the next test vacuous."""
    found = _exit_call_sites()
    assert len(found) >= 8, f"suspiciously few exit sites: {found}"

    files = {path.name for path, _ in found}
    assert "main.py" in files, "the root app exits on domain errors"
    assert "helmrelease.py" in files, "promote/monitor/test all exit nonzero"

    # And that the check itself can see a literal: if `_literal_code` ever
    # stopped recognising one, the rule below would pass by blindness.
    assert _literal_code(ast.parse("typer.Exit(code=1)").body[0].value) == 1  # type: ignore[attr-defined]
    assert _literal_code(ast.parse("sys.exit(127)").body[0].value) == 127  # type: ignore[attr-defined]
    assert _literal_code(ast.parse("sys.exit(exit_code_for(x))").body[0].value) is None  # type: ignore[attr-defined]


def test_no_module_outside_the_table_writes_a_nonzero_exit_literal() -> None:
    """Every nonzero exit must name an `Outcome`, not a number.

    A magic literal is not a style complaint here: it is a second, silent
    copy of the table that no test transcribes and no reader can find. The
    2 -> 4 move for tool errors is exactly what such a copy would have
    survived unchanged.

    `typer.Exit(0)` and `sys.exit(0)` are fine -- zero is the absence of a
    failure, not a classification of one, and spelling it
    `exit_code_for(Outcome.SUCCESS)` buys nothing.
    """
    offenders: list[str] = []
    for path, node in _exit_call_sites():
        rel = path.relative_to(_SRC.parent)
        if rel == _TABLE:
            continue
        code = _literal_code(node)
        if code:
            offenders.append(f"  {rel}:{node.lineno} -> exit {code}")

    assert not offenders, (
        "a nonzero exit code must come from "
        "`chart_manager.plumbing.exit_codes.exit_code_for(Outcome.…)`, "
        "never from a literal:\n"
        + "\n".join(offenders)
        + "\n\nPick the row that describes what happened (FAILED, USAGE, SPEC, "
        "TOOL, ENVIRONMENT, MISSING_BINARY) and let the table say what it is worth."
    )


# --------------------------------------------------------------------------
# behavioral: what `main()` does with an exception that reaches it
# --------------------------------------------------------------------------


def _exit_code_from_main(exc: BaseException, monkeypatch: pytest.MonkeyPatch) -> int:
    """Run `cli.main()` with an app that raises `exc`, and return its exit code.

    Driven through `main()` itself rather than `conftest.cli()`: CliRunner
    invokes the Typer app, so it never reaches the `except` arms that are
    the entire subject here.
    """
    from chart_manager.cli import main as main_cli

    def _raise() -> None:
        raise exc

    monkeypatch.setattr(main_cli, "app", _raise)
    with pytest.raises(SystemExit) as caught:
        main_cli.main()
    assert isinstance(caught.value.code, int)
    return caught.value.code


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (MissingToolError("helm not found"), 127),
        (ExternalCommandError("helm template exploded"), 4),
        (CommandTimeout("kubeconform timed out"), 4),
        (SpecError("chart-lifecycle.yaml is not valid"), 3),
        (DependencyCycleError("a -> b -> a"), 3),
        (CapabilityUnavailableError("cluster tests are disabled"), 1),
        (ChartNotFoundError("chart not found: nope"), 1),
        (ChartManagerError("something went wrong"), 1),
    ],
)
def test_a_domain_error_exits_with_the_code_its_type_earns(
    exc: ChartManagerError, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before this, all eight of these exited 1 except the missing binary.

    The distinctions are the product: "your yaml is wrong" (3), "helm ran
    and failed" (4) and "the run failed" (1) send an operator to three
    different places, and a pipeline can branch on them.
    """
    assert _exit_code_from_main(exc, monkeypatch) == expected


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (FileNotFoundError(2, "No such file or directory", "values.yaml"), 1),
        (IsADirectoryError(21, "Is a directory", "charts/"), 5),
        (PermissionError(13, "Permission denied", "/etc/shadow"), 5),
        (ConnectionRefusedError(61, "Connection refused"), 5),
    ],
)
def test_an_os_error_becomes_a_mapped_code_and_never_a_traceback(
    exc: OSError, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design doc 8.9's general case.

    `IsADirectoryError` is the one that was reported: `grafana
    lint-dashboards --path DIR` printed a Python traceback and exited on
    Python's terms. A missing *data* file stays 1 so that 127 keeps meaning
    "install the binary"; everything else is the environment refusing (5).
    """
    assert _exit_code_from_main(exc, monkeypatch) == expected


def test_the_error_line_reads_like_a_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard the guard: a mapped exit code with no message is still a dead end."""
    from chart_manager.cli import main as main_cli

    assert main_cli._os_error_text(IsADirectoryError(21, "Is a directory", "charts/")) == (
        "is a directory: charts/"
    )
    assert main_cli._os_error_text(OSError()) == str(OSError())


def test_module_docstring_transcribes_every_row() -> None:
    """Doc drift here is worse than none: the table is read by humans first."""
    import chart_manager.plumbing.exit_codes as mod

    doc = mod.__doc__ or ""
    for outcome, code in EXIT_CODE.items():
        row = rf"\|\s*{code}\s*\|\s*{outcome.name}\s*\|"
        assert re.search(row, doc), f"missing docstring row for {outcome.name} -> {code}"
