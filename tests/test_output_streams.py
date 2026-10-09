"""Executable form of the output-stream contract.

The rule, stated once so it is never re-derived:

    The command's selected --output projection goes to stdout; everything
    else goes to stderr.

This is deliberately more precise than "stdout is data, stderr is
narration". A human-readable table *is* the selected projection when the
format resolves to text, so it belongs on stdout. Get that backwards and
`chart-manager chart list | less` shows an empty page. What belongs on
stderr is everything the caller did not ask for as output: progress,
warnings, access hints, deprecation notices, and error detail.

Why this is worth a gate rather than a convention:

  (a) `.github/workflows/ci.yaml` captures CLI stdout into shell variables
      (`publish_charts="$(... plan --for publish ...)"`). A warning printed
      on the same stream is silently absorbed into the value, and no exit
      code reveals it.

  (b) `chart validate -o json` writes a JSON document to stdout. It
      used to write its warnings to a stdout console too, so
      `-o json --github-step-summary` with `$GITHUB_STEP_SUMMARY`
      unset emitted a warning *inside* the JSON stream. That is the exact
      regression `test_json_projections_are_parseable_on_stdout` exists to
      catch, and it is why the tests below parse rather than
      pattern-match.

These tests drive real commands through Typer's CliRunner (which separates
`.stdout` from `.stderr`) and assert the split holds end to end. A new module
that constructs its own `Console` is caught by ruff's TID251 ban instead.

Note on `Console(file=...)` vs `Console(stderr=...)`: Rich resolves `file=`
once, at construction. A module-level console built that way captures the
real stdout at import and ignores any later replacement of `sys.stdout` --
including CliRunner's, which is why such output is invisible to these
tests. `cli/streams.py` therefore uses `stderr=False|True`, which Rich
resolves lazily on every write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import cli, write_workspace


@pytest.fixture
def root(root: Path) -> Path:
    """An empty repository root: every command below is cluster-free."""
    write_workspace(root)
    return root


def _argv(name: str) -> list[str]:
    """Commands that reach a real projection without a cluster or network."""
    return {
        "validate-json": [
            "chart", "validate", "--all", "--output", "json",
            "--progress", "none",
        ],
        # Deliberately does NOT name `--output json`: it lets `auto` resolve
        # to json, which is what happens off a terminal and therefore what
        # happens in CI. An *explicit* `-o json` implies `--quiet`,
        # which would suppress the very warning this case exists to
        # produce -- and the regression being guarded is a warning landing in
        # the JSON document, which only has teeth while the warning is emitted.
        # See `cli/output.resolve` for why auto-resolved json is not quiet.
        "validate-json-with-warning": [
            "chart", "validate", "--all",
            "--progress", "none", "--github-step-summary",
        ],
        "chart-test-matrix": ["plan", "-o", "github", "--all"],
    }[name]


@pytest.mark.parametrize(
    "command",
    ["validate-json", "validate-json-with-warning", "chart-test-matrix"],
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
    result = cli(*_argv(command))

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
    result = cli(*_argv("validate-json-with-warning"))

    assert "GITHUB_STEP_SUMMARY" in result.stderr
    assert "GITHUB_STEP_SUMMARY" not in result.stdout


def _narration_case(name: str, root: Path) -> tuple[list[str], str]:
    """(argv, the narration fragment it must print) for cluster-free commands."""
    if name == "nothing-to-clean":
        return ["chart", "cache", "clean"], "nothing to clean"
    if name == "cleaned":
        (root / ".chart-manager" / "rendered").mkdir(parents=True)
        return ["chart", "cache", "clean"], "cleaned:"
    if name == "no-dashboards":
        return ["grafana", "dashboard", "lint"], "no dashboards found"
    raise AssertionError(f"unknown narration case: {name}")


@pytest.mark.parametrize("case", ["nothing-to-clean", "cleaned", "no-dashboards"])
def test_narration_goes_to_stderr_and_never_to_stdout(case: str, root: Path) -> None:
    """Status lines are not a projection, so nothing may pipe them."""
    argv, fragment = _narration_case(case, root)
    result = cli(*argv)

    assert fragment in result.stderr, result.output
    assert fragment not in result.stdout


def test_a_command_with_no_projection_writes_nothing_to_stdout(root: Path) -> None:
    """`chart cache clean` produces no document, so stdout must be empty.

    This is the property that makes `cmd >/dev/null` a safe way to silence
    a mutating command without also silencing its errors.
    """
    result = cli("chart", "cache", "clean")

    assert result.stdout == ""
    assert result.stderr != ""
