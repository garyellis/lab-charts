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


# Deliberately NOT tested here: that no `typer.Exit(code=<literal>)` survives
# in `cli/`. Four do (`cli/helmrelease.py`, `cli/publish.py`,
# `cli/validate.py`), all spelling exit 1 correctly. Routing them through
# `Outcome` is P2.1's job -- it is the commit that maps every
# `ChartManagerError` subclass in `main()` -- and a gate added here would
# fail on code this change does not touch.


def test_module_docstring_transcribes_every_row() -> None:
    """Doc drift here is worse than none: the table is read by humans first."""
    import chart_manager.plumbing.exit_codes as mod

    doc = mod.__doc__ or ""
    for outcome, code in EXIT_CODE.items():
        row = rf"\|\s*{code}\s*\|\s*{outcome.name}\s*\|"
        assert re.search(row, doc), f"missing docstring row for {outcome.name} -> {code}"
