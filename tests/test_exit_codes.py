"""The exit-code table is the process contract; pin it as data.

`plumbing/exit_codes.py` is the only module allowed to say what number an
outcome is worth, which makes it the only place a silent renumbering could
happen. These tests are deliberately literal: an expectation derived from
the table under test would pass no matter what the table said.
"""

from __future__ import annotations

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
    Outcome,
    exit_code_for,
)


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
        # promote's wire `ok` (outcome is SUCCESS) agrees with `$?` only because SUCCESS alone is 0.
        (Outcome.SUCCESS, 0),
        (Outcome.FAILED, 1),
        (Outcome.USAGE, 2),
        (Outcome.SPEC, 3),
        (Outcome.TOOL, 4),
        (Outcome.ENVIRONMENT, 5),
        (Outcome.MISSING_BINARY, 127),
    ],
)
def test_each_outcome_maps_to_its_exit_code(
    outcome: Outcome,
    expected: int,
) -> None:
    """The exit-code table, transcribed. Changing a row is a release event."""
    assert exit_code_for(outcome) == expected


# --------------------------------------------------------------------------
# behavioral: what `main()` does with an exception that reaches it
# --------------------------------------------------------------------------


def _exit_code_from_main(exc: BaseException, monkeypatch: pytest.MonkeyPatch) -> int:
    """Run `main.main()` with an app that raises `exc`, and return its exit code.

    Driven through `main()` itself rather than `conftest.cli()`: CliRunner
    invokes the Typer app, so it never reaches the `except` arms that are
    the entire subject here.
    """
    from chart_manager import main as main_cli

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
        (CapabilityUnavailableError("chart tests are disabled"), 1),
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
    """The general case of an `OSError` escaping a command.

    `IsADirectoryError` is the one that was reported: `grafana
    lint-dashboards --path DIR` printed a Python traceback and exited on
    Python's terms. A missing *data* file stays 1 so that 127 keeps meaning
    "install the binary"; everything else is the environment refusing (5).
    """
    assert _exit_code_from_main(exc, monkeypatch) == expected


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (IsADirectoryError(21, "Is a directory", "charts/"), "error: is a directory: charts/"),
        (OSError("socket closed"), "error: socket closed"),
    ],
)
def test_the_error_line_reads_like_a_sentence(
    exc: OSError, expected: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mapped exit code with no readable message is still a dead end."""
    _exit_code_from_main(exc, monkeypatch)

    assert capsys.readouterr().err.strip() == expected
