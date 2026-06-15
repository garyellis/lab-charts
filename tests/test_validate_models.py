from __future__ import annotations

from pathlib import Path

from chart_manager.plumbing.validate_models import (
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


def test_exit_code_zero_when_all_pass_or_skip() -> None:
    result = _run(
        _row(
            render=PhaseResult(phase="render", status="PASS"),
            schema=PhaseResult(phase="schema", status="SKIP"),
            policy=PhaseResult(phase="policy", status="NOT_RUN"),
        )
    )
    assert result.exit_code() == 0


def test_exit_code_one_on_validation_failure() -> None:
    result = _run(
        _row(
            render=PhaseResult(phase="render", status="PASS"),
            schema=PhaseResult(phase="schema", status="FAIL", detail="bad replicas"),
        )
    )
    assert result.exit_code() == 1


def test_exit_code_two_on_tool_error() -> None:
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
    assert result.exit_code() == 2


def test_exit_code_three_on_spec_errors_list() -> None:
    result = _run(spec_errors=("corrupt validate-spec.yaml",))
    assert result.exit_code() == 3


def test_exit_code_three_on_spec_error_in_phase() -> None:
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
    assert result.exit_code() == 3


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
    assert result.exit_code() == 2


def test_empty_run_exits_zero() -> None:
    result = _run()
    assert result.exit_code() == 0
