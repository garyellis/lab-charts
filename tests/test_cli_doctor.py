"""`chart-manager doctor`: the three things the surface owns.

Argument shape, projection, exit code -- and nothing else, which is the
property most of these tests are really asserting. Every one of them
replaces the whole provider set through `_make_doctor_service`, so no test
here touches PATH, a cluster, or a network. That is possible only because
the checks live behind a service seam rather than inside the command body;
a `doctor` that probed inline could not be tested without the toolchain
installed, which is the shape the design forbids.

The checks themselves, and who owns them, are in
`tests/test_doctor_service.py`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from chart_manager.cli import doctor as doctor_cli
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
from chart_manager.services.doctor import DoctorService

from .conftest import cli

_HEALTHY = Check.ok("helm", "v3.16.2 (/opt/bin/helm)")
_MISSING = Check.failed(
    "kubeconform",
    "kubeconform not found on PATH",
    remediation="install kubeconform",
    outcome=Outcome.MISSING_BINARY,
)
_UNREACHABLE = Check.failed(
    "events-backend",
    "dynamodb table lifecycle-events unavailable",
    remediation="check AWS credentials",
    outcome=Outcome.ENVIRONMENT,
)


@pytest.fixture
def fake_doctor(monkeypatch: pytest.MonkeyPatch):
    """Replace the container-built service with one over scripted checks.

    Same seam as `tests/test_cli_helmrelease.py` uses for the promote
    services: the command keeps its real body, only the wiring is faked.
    """

    def install(*checks: Check, capability: str = "helm") -> None:
        provider: dict[str, object] = {capability: lambda: checks}
        service = DoctorService(provider)  # type: ignore[arg-type]
        monkeypatch.setattr(doctor_cli, "_make_doctor_service", lambda: service)

    return install


def _providers(mapping: dict[str, Sequence[Check]]) -> DoctorService:
    """A DoctorService over several capabilities at once."""
    return DoctorService({name: (lambda c=checks: c) for name, checks in mapping.items()})


# --- exit codes -------------------------------------------------------------


def test_a_clean_preflight_exits_zero(fake_doctor) -> None:
    """The case every other assertion here is measured against."""
    fake_doctor(_HEALTHY)

    result = cli("doctor")

    assert result.exit_code == EXIT_SUCCESS, result.output


def test_a_missing_binary_exits_127(fake_doctor) -> None:
    """The shell's own "command not found", straight out of the exit-code table."""
    fake_doctor(_MISSING)

    result = cli("doctor")

    assert result.exit_code == EXIT_MISSING_BINARY


def test_an_unreachable_backend_exits_5(fake_doctor) -> None:
    """ENVIRONMENT, not FAILED: nothing the caller asked for went wrong."""
    fake_doctor(_UNREACHABLE)

    result = cli("doctor")

    assert result.exit_code == EXIT_ENVIRONMENT


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
    """No exit-code literal lives in `cli/doctor.py`; this is what that buys.

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


# --- --for ------------------------------------------------------------------


def test_for_runs_only_the_capabilities_that_command_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--for chart validate` must not wait on docker or an events backend."""
    service = _providers(
        {
            "helm": (_HEALTHY,),
            "kubeconform": (Check.ok("kubeconform", "v0.6.7"),),
            "kyverno": (Check.ok("kyverno", "v1.13.0"),),
            "events": (_UNREACHABLE,),
        }
    )
    monkeypatch.setattr(doctor_cli, "_make_doctor_service", lambda: service)

    result = cli("doctor", "--for", "chart validate", "-o", "json")

    payload = json.loads(result.stdout)
    assert payload["for"] == "chart validate"
    assert [check["name"] for check in payload["checks"]] == ["helm", "kubeconform", "kyverno"]
    assert result.exit_code == EXIT_SUCCESS, "the unreachable backend was out of scope"


def test_an_unknown_for_target_is_a_usage_error_that_lists_the_real_ones(
    fake_doctor,
) -> None:
    """Exit 2 and a list beats exit 5 for a command the user mistyped."""
    fake_doctor(_HEALTHY)

    result = cli("doctor", "--for", "chart valdiate")

    assert result.exit_code == EXIT_USAGE
    assert "chart validate" in result.output


# --- registration -----------------------------------------------------------


def test_doctor_is_a_root_command() -> None:
    """A preflight is about the process, not about one group."""
    result = cli("--help")

    assert "doctor" in result.stdout
