"""`chart-manager doctor`: exit codes and output modes over a scripted report."""

from __future__ import annotations

import json

import pytest

from chart_manager.commands.doctor import DoctorReport
from chart_manager.commands.doctor import cli as doctor_cli
from chart_manager.plumbing.exit_codes import (
    EXIT_ENVIRONMENT,
    EXIT_MISSING_BINARY,
    EXIT_SPEC,
    EXIT_SUCCESS,
    EXIT_TOOL,
    EXIT_USAGE,
    Outcome,
)
from chart_manager.plumbing.preflight import Check
from tests.conftest import cli

_HEALTHY = Check.ok("helm", "v3.16.2 (/opt/bin/helm)")
_MISSING = Check.failed(
    "kubeconform",
    "kubeconform not found on PATH",
    remediation="install kubeconform",
    outcome=Outcome.MISSING_BINARY,
)


@pytest.fixture
def fake_doctor(monkeypatch: pytest.MonkeyPatch):
    """Make `doctor` report exactly these checks."""

    def install(*checks: Check) -> None:
        monkeypatch.setattr(doctor_cli, "run", lambda **_: DoctorReport(checks=checks))

    return install


# --- exit codes -------------------------------------------------------------


def test_a_clean_preflight_exits_zero(fake_doctor) -> None:
    """The case every other assertion here is measured against."""
    fake_doctor(_HEALTHY)

    result = cli("doctor")

    assert result.exit_code == EXIT_SUCCESS, result.output


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (Outcome.SPEC, EXIT_SPEC),
        (Outcome.TOOL, EXIT_TOOL),
        (Outcome.ENVIRONMENT, EXIT_ENVIRONMENT),
        (Outcome.MISSING_BINARY, EXIT_MISSING_BINARY),
    ],
)
def test_every_failure_outcome_goes_through_the_exit_code_table(
    fake_doctor, outcome: Outcome, expected: int
) -> None:
    """No exit-code literal lives in `commands/doctor/cli.py`; this is what that buys.

    Parametrised over the whole failing half of `Outcome` so a future check
    that reports a different one cannot exit with a number nobody chose.
    """
    fake_doctor(Check.failed("x", "broken", remediation="fix it", outcome=outcome))

    assert cli("doctor").exit_code == expected


def test_a_skipped_check_does_not_make_the_command_fail(fake_doctor) -> None:
    """`EVENTS_BACKEND=none` is the real case: supported, and not a problem."""
    fake_doctor(_HEALTHY, Check.skipped("events-backend", "telemetry disabled"))

    assert cli("doctor").exit_code == EXIT_SUCCESS


# --- projections ------------------------------------------------------------


def test_json_is_the_only_thing_on_stdout(fake_doctor) -> None:
    """`chart-manager doctor -o json | jq` must not choke on a summary line."""
    fake_doctor(_HEALTHY, _MISSING)

    result = cli("doctor", "-o", "json")

    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["outcome"] == "missing-binary"
    assert payload["for"] is None
    assert [check["name"] for check in payload["checks"]] == ["helm", "kubeconform"]


def test_the_json_check_shape_is_the_documented_four_keys(fake_doctor) -> None:
    """name / status / detail / remediation. A consumer may rely on all four."""
    fake_doctor(_MISSING)

    payload = json.loads(cli("doctor", "-o", "json").stdout)

    assert payload["checks"][0] == {
        "name": "kubeconform",
        "status": "failed",
        "detail": "kubeconform not found on PATH",
        "remediation": "install kubeconform",
    }


def test_the_table_carries_the_remediation_beside_the_failure(fake_doctor) -> None:
    """A hint the operator has to scroll for is one they will not read."""
    fake_doctor(_MISSING)

    result = cli("doctor", "-o", "table")

    assert "kubeconform" in result.stdout
    assert "install kubeconform" in result.stdout


def test_the_summary_line_is_narration_and_stays_off_stdout(fake_doctor) -> None:
    """The table is the projection; `doctor | grep FAIL` must not match a summary."""
    fake_doctor(_HEALTHY, _MISSING)

    result = cli("doctor", "-o", "table")

    assert "1 of 2 checks failed" in result.stderr
    assert "checks failed" not in result.stdout


def test_the_global_output_flag_reaches_doctor(fake_doctor) -> None:
    """`doctor` opts into the shared vocabulary rather than owning a flag."""
    fake_doctor(_HEALTHY)

    result = cli("-o", "json", "doctor")

    assert json.loads(result.stdout)["ok"] is True


def test_a_projection_doctor_cannot_produce_is_a_usage_error(fake_doctor) -> None:
    """There is no yaml rendering of a preflight, so asking is exit 2, not silence."""
    fake_doctor(_HEALTHY)

    result = cli("doctor", "-o", "yaml")

    assert result.exit_code == EXIT_USAGE
