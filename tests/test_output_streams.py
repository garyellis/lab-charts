"""Executable form of the output-stream contract.

The rule, stated once so it is never re-derived:

    The command's selected --output projection goes to stdout; everything
    else goes to stderr.

This is deliberately more precise than "stdout is data, stderr is
narration". A human-readable table *is* the selected projection when the
format resolves to text, so it belongs on stdout. Get that backwards and
`chart-manager charts list | less` shows an empty page. What belongs on
stderr is everything the caller did not ask for as output: progress,
warnings, access hints, deprecation notices, and error detail.

Why this is worth a gate rather than a convention:

  (a) `.github/workflows/ci.yaml` captures CLI stdout into shell variables
      (`publish_charts="$(... ci publish-charts ...)"`). A warning printed
      on the same stream is silently absorbed into the value, and no exit
      code reveals it.

  (b) `cli/validate.py --format json` writes a JSON document to stdout. It
      used to write its warnings to a stdout console too, so
      `--format json --github-step-summary` with `$GITHUB_STEP_SUMMARY`
      unset emitted a warning *inside* the JSON stream. That is the exact
      regression `test_json_projections_are_parseable_on_stdout` exists to
      catch, and it is why the behavioral leg below parses rather than
      pattern-matches.

Two legs, because either alone is insufficient:

  1. BEHAVIORAL -- drives real commands through Typer's CliRunner (which
     separates `.stdout` from `.stderr`) and asserts the split holds end to
     end. Catches a regression in code that already exists.

  2. STATIC -- AST-scans `cli/` for a `Console(...)` that does not name its
     stream. Catches a *new* module written by someone who never read this
     file, which the behavioral leg cannot do because it only knows about
     the commands it enumerates.

Note on `Console(file=...)` vs `Console(stderr=...)`: Rich resolves `file=`
once, at construction. A module-level console built that way captures the
real stdout at import and ignores any later replacement of `sys.stdout` --
including CliRunner's, which is why such output is invisible to these
tests. `cli/streams.py` therefore uses `stderr=False|True`, which Rich
resolves lazily on every write. Either form satisfies the static rule; a
bare `Console()` does not, because it silently means stdout.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chart_manager.cli.main import app

_SRC = Path(__file__).resolve().parents[1] / "src"
_CLI = _SRC / "chart_manager" / "cli"

#: A console must say which stream it writes to, one way or the other.
_STREAM_KEYWORDS = frozenset({"file", "stderr"})


# --------------------------------------------------------------------------
# (1) behavioral: the split holds when a real command runs
# --------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """An empty repository root: every command below is cluster-free."""
    return tmp_path


def _argv(name: str, root: Path) -> list[str]:
    """Commands that reach a real projection without a cluster or network."""
    return {
        "validate-json": [
            "validate", "run", "--all", "--format", "json",
            "--progress", "none", "--root", str(root),
        ],
        "validate-json-with-warning": [
            "validate", "run", "--all", "--format", "json",
            "--progress", "none", "--github-step-summary", "--root", str(root),
        ],
        "cluster-test-matrix": [
            "ci", "cluster-test-matrix", "--all", "--root", str(root),
        ],
    }[name]


@pytest.mark.parametrize(
    "command",
    ["validate-json", "validate-json-with-warning", "cluster-test-matrix"],
)
def test_json_projections_are_parseable_on_stdout(
    command: str, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A json projection must be the *only* thing on stdout.

    `validate-json-with-warning` is the regression case: it asks for JSON
    and simultaneously triggers a warning. Before the stream split the
    warning landed between the JSON document and the end of stdout, so
    `json.loads` raised -- exactly what a `| jq` consumer would hit.
    """
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    result = CliRunner().invoke(app, _argv(command, root))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)


def test_the_warning_case_actually_warns(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard the guard: prove the regression case still emits narration.

    Without this, someone could delete the warning and
    `test_json_projections_are_parseable_on_stdout` would keep passing
    while no longer testing anything.
    """
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    result = CliRunner().invoke(app, _argv("validate-json-with-warning", root))

    assert "GITHUB_STEP_SUMMARY" in result.stderr
    assert "GITHUB_STEP_SUMMARY" not in result.stdout


def _narration_case(name: str, root: Path) -> tuple[list[str], str]:
    """(argv, the narration fragment it must print) for cluster-free commands."""
    if name == "nothing-to-clean":
        return ["validate", "clean", "--root", str(root)], "nothing to clean"
    if name == "cleaned":
        (root / ".chart-manager" / "rendered").mkdir(parents=True)
        return ["validate", "clean", "--root", str(root)], "cleaned:"
    if name == "no-dashboards":
        return ["grafana", "lint-dashboards", "--root", str(root)], "no dashboards found"
    raise AssertionError(f"unknown narration case: {name}")


@pytest.mark.parametrize("case", ["nothing-to-clean", "cleaned", "no-dashboards"])
def test_narration_goes_to_stderr_and_never_to_stdout(case: str, root: Path) -> None:
    """Status lines are not a projection, so nothing may pipe them."""
    argv, fragment = _narration_case(case, root)
    result = CliRunner().invoke(app, argv)

    assert fragment in result.stderr, result.output
    assert fragment not in result.stdout


def test_a_command_with_no_projection_writes_nothing_to_stdout(root: Path) -> None:
    """`validate clean` produces no document, so stdout must be empty.

    This is the property that makes `cmd >/dev/null` a safe way to silence
    a mutating command without also silencing its errors.
    """
    result = CliRunner().invoke(app, ["validate", "clean", "--root", str(root)])

    assert result.stdout == ""
    assert result.stderr != ""


# --------------------------------------------------------------------------
# (2) static: every console in cli/ names its stream
# --------------------------------------------------------------------------


def _console_constructions() -> list[tuple[Path, ast.Call]]:
    """Every `Console(...)` call site under cli/, as (path, node)."""
    found: list[tuple[Path, ast.Call]] = []
    for path in sorted(_CLI.rglob("*.py")):
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
            if name == "Console":
                found.append((path, node))
    return found


def test_console_scan_finds_the_constructions_it_is_meant_to_check() -> None:
    """Guard the guard: an empty sweep would make the next test vacuous."""
    found = _console_constructions()
    assert len(found) >= 3, f"suspiciously few Console() sites: {found}"

    files = {path.name for path, _ in found}
    assert "streams.py" in files, "the shared seam should construct consoles"
    assert "validate_progress.py" in files, "the progress displays construct their own"


def test_every_console_in_cli_names_its_stream() -> None:
    """A bare `Console()` silently means stdout -- the defect this commit fixed.

    Passing `file=` or `stderr=` forces the author to decide, at the point
    of construction, whether what follows is the caller's data or the
    operator's narration. `cli/streams.py` exists so the answer is usually
    `data_console()` / `narration_console()` rather than a raw Console.
    """
    offenders: list[str] = []
    for path, node in _console_constructions():
        named = {kw.arg for kw in node.keywords if kw.arg is not None}
        if not (named & _STREAM_KEYWORDS):
            rel = path.relative_to(_SRC)
            offenders.append(f"  {rel}:{node.lineno}")

    assert not offenders, (
        "every Console() under cli/ must name its stream explicitly "
        "(file=... or stderr=...), because a bare Console() defaults to "
        "stdout and will corrupt a --output json payload:\n"
        + "\n".join(offenders)
        + "\n\nPrefer chart_manager.cli.streams.data_console() for the "
        "selected projection and narration_console() for everything else."
    )


def test_streams_module_exposes_the_seam() -> None:
    """Guard the guard: the rule above is only useful if the seam exists."""
    from chart_manager.cli import streams

    assert callable(streams.data_console)
    assert callable(streams.narration_console)
    assert callable(streams.narrate)
    # The two must not resolve to the same stream.
    assert streams.data_console().stderr is False
    assert streams.narration_console().stderr is True
