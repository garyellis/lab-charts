"""Manifest-validation domain model tests."""

from __future__ import annotations

from pathlib import Path

from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.services.manifest_validation.models import (
    PhaseResult,
    RowResult,
    RunResult,
    WorklistRow,
)


def _row(**phase_results: PhaseResult) -> RowResult:
    return RowResult(
        row=WorklistRow(chart="c", env="e", release="r", namespace="n"),
        phases=phase_results,
    )


def _run(*rows: RowResult, spec_errors: tuple[str, ...] = ()) -> RunResult:
    return RunResult(rows=tuple(rows), rendered_root=Path("/tmp/x"), spec_errors=spec_errors)


def test_outcome_is_success_when_all_pass_or_skip() -> None:
    result = _run(
        _row(
            render=PhaseResult(phase="render", status="PASS"),
            schema=PhaseResult(phase="schema", status="SKIP"),
            policy=PhaseResult(phase="policy", status="NOT_RUN"),
        )
    )
    assert result.outcome() is Outcome.SUCCESS


def test_outcome_is_failed_on_validation_failure() -> None:
    result = _run(
        _row(
            render=PhaseResult(phase="render", status="PASS"),
            schema=PhaseResult(phase="schema", status="FAIL", detail="bad replicas"),
        )
    )
    assert result.outcome() is Outcome.FAILED


def test_outcome_is_tool_on_tool_error() -> None:
    result = _run(
        _row(
            render=PhaseResult(
                phase="render",
                status="FAIL",
                detail="helm crashed",
                error_type="tool",
            ),
        )
    )
    assert result.outcome() is Outcome.TOOL


def test_outcome_is_spec_on_spec_errors_list() -> None:
    result = _run(spec_errors=("corrupt chart-lifecycle.yaml",))
    assert result.outcome() is Outcome.SPEC


def test_outcome_is_spec_on_spec_error_in_phase() -> None:
    result = _run(
        _row(
            render=PhaseResult(
                phase="render",
                status="FAIL",
                detail="unknown spec version 99",
                error_type="spec",
            ),
        )
    )
    assert result.outcome() is Outcome.SPEC


def test_tool_error_takes_precedence_over_plain_fail() -> None:
    result = _run(
        _row(
            render=PhaseResult(
                phase="render",
                status="FAIL",
                detail="helm boom",
                error_type="tool",
            ),
        ),
        _row(
            schema=PhaseResult(phase="schema", status="FAIL", detail="bad replicas"),
        ),
    )
    assert result.outcome() is Outcome.TOOL


def test_empty_run_is_success() -> None:
    result = _run()
    assert result.outcome() is Outcome.SUCCESS
