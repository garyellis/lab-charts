"""`chart validate` at the CLI: flags, output modes and exit codes, with `run()` faked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_manager.commands import validate
from chart_manager.commands.validate import cli as validate_cli
from chart_manager.plumbing.errors import MissingToolError
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
from tests.conftest import cli, write_workspace

PASSED = validate.CheckResult("passed")


def outcome_with(status: str = "passed", **extra) -> validate.ValidateOutcome:  # type: ignore[no-untyped-def]
    checks = {"render": PASSED, "schema": validate.CheckResult(status, "Widget/demo: bad")}
    return validate.ValidateOutcome(
        rows=(validate.Row("demo", "dev", "demo", "lab-dev", checks),), **extra
    )


class FakeRun:
    """Stands in for `validate.run`: records each request and answers with `result`."""

    def __init__(self) -> None:
        self.requests: list[validate.ValidateRequest] = []
        self.result: validate.ValidateOutcome | Exception = outcome_with()

    def __call__(self, request, **_):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        request.out.mkdir(parents=True, exist_ok=True)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def fake_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    write_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    fake = FakeRun()
    monkeypatch.setattr(validate_cli, "run", fake)
    return fake


def test_flags_become_the_request(fake_run, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    changed = tmp_path / "changed.txt"
    changed.write_text("charts/demo/values.yaml\n\n")

    result = cli(
        "chart",
        "validate",
        "--chart",
        "demo",
        "--env",
        "dev",
        "--check",
        "render",
        "--changed-files",
        str(changed),
        "--workers",
        "3",
        "--fail-fast",
        "--tool-timeout",
        "5",
        "--verbose",
        "--progress",
        "none",
        "-o",
        "json",
    )

    assert result.exit_code == 0, result.output
    (request,) = fake_run.requests
    assert (request.charts, request.envs, request.checks, request.changes) == (
        ("demo",),
        ("dev",),
        frozenset({"render"}),
        ("charts/demo/values.yaml",),
    )
    assert (request.workers, request.fail_fast, request.tool_timeout, request.verbose) == (
        3,
        True,
        5.0,
        True,
    )


@pytest.mark.parametrize(
    ("argv", "charts", "changes"),
    [(("--all",), (), None), (("demo",), ("demo",), None)],
    ids=["all", "named-chart"],
)
def test_all_or_a_named_chart_skips_change_detection(fake_run, argv, charts, changes) -> None:  # type: ignore[no-untyped-def]
    assert cli("chart", "validate", *argv, "-o", "json").exit_code == 0
    assert (fake_run.requests[0].charts, fake_run.requests[0].changes) == (charts, changes)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (outcome_with("passed"), Outcome.SUCCESS),
        (outcome_with("failed"), Outcome.FAILED),
        (outcome_with("error"), Outcome.TOOL),
        (outcome_with("passed", spec_errors=("broken: bad",)), Outcome.SPEC),
    ],
    ids=["passed", "failed", "tool-error", "spec-error"],
)
def test_the_exit_code_follows_the_outcome(fake_run, result, expected) -> None:  # type: ignore[no-untyped-def]
    fake_run.result = result

    assert cli("chart", "validate", "--all", "-o", "json").exit_code == exit_code_for(expected)


def test_a_missing_binary_reaches_main_unchanged(fake_run) -> None:  # type: ignore[no-untyped-def]
    """`cli/main.py` maps MissingToolError to exit 127 (see test_exit_codes.py)."""
    fake_run.result = MissingToolError("required tool not found on PATH: helm")

    result = cli("chart", "validate", "--all", "-o", "json")

    assert isinstance(result.exception, MissingToolError)


def test_json_output_lists_each_row_with_its_checks(fake_run) -> None:  # type: ignore[no-untyped-def]
    fake_run.result = outcome_with("failed")

    payload = json.loads(cli("chart", "validate", "--all", "-o", "json").stdout)

    assert payload["exit_code"] == exit_code_for(Outcome.FAILED)
    assert payload["rows"][0]["checks"]["schema"] == {
        "status": "failed",
        "detail": "Widget/demo: bad",
        "elapsed_seconds": None,
    }


def test_output_all_writes_the_summaries_and_the_step_summary(
    fake_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    step_summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    fake_run.result = outcome_with("failed")

    result = cli(
        "chart",
        "validate",
        "--all",
        "-o",
        "all",
        "--keep",
        "--github-step-summary",
        "--progress",
        "none",
    )

    out = fake_run.requests[0].out
    assert result.exit_code == exit_code_for(Outcome.FAILED)
    assert "demo" in result.stdout
    assert (out / "summary.md").read_text() == step_summary.read_text()
    assert json.loads((out / "summary.json").read_text())["rows"][0]["chart"] == "demo"


@pytest.mark.parametrize(
    ("status", "argv", "kept"),
    [("passed", (), False), ("passed", ("--keep",), True), ("failed", (), True)],
    ids=["clean-run-removed", "keep", "failed-run-kept"],
)
def test_the_render_dir_is_kept_only_when_asked_or_when_the_run_failed(
    fake_run, status, argv, kept
) -> None:  # type: ignore[no-untyped-def]
    fake_run.result = outcome_with(status)

    cli("chart", "validate", "--all", "-o", "json", *argv)

    assert fake_run.requests[0].out.exists() is kept
