"""Wire contract for manifest-validation results.

This module is the single source of truth for the machine-readable
projection of a `RunResult`: the jq-friendly JSON payload. Every surface --
the CLI's `--output json`, a REST endpoint, a CI artifact consumer --
projects through `to_json` so they cannot diverge.

What `to_json` and `markdown.py` share -- the pass/fail tally, the empty-run
explanation, the elapsed-time column -- lives in `models.py`.

Deliberately Rich-free and I/O-free. Nothing here may import `rich`: an HTTP
server has no terminal, and `to_json` must not drag a TUI library into a
worker process. Terminal rendering (Rich tables, color styles, console
markup) lives in `cli/validate_render.py`; a test in
`tests/test_manifest_validation_rendering.py` asserts that importing this module leaves
`rich` out of `sys.modules`.
"""

from __future__ import annotations

from chart_manager.plumbing.exit_codes import exit_code_for
from chart_manager.services.manifest_validation.models import (
    RunOutcome,
    no_work_reason,
)

__all__ = [
    "to_json",
]


def to_json(
    outcome: RunOutcome,
    *,
    requested_charts: tuple[str, ...] = (),
    requested_environments: tuple[str, ...] = (),
) -> dict[str, object]:
    """Render a run outcome as a stable, jq-friendly dict.

    Uses str(Path) for any path so json.dumps works without a custom
    encoder.

    `elapsed_seconds` is always present (null when the phase didn't run)
    so downstream tooling can rely on the key existing regardless of
    --timings. Rounded to ms so two runs of the same workload diff
    cleanly.
    """
    result = outcome.result
    diagnostics = _diagnostics(
        outcome,
        requested_charts=requested_charts,
        requested_environments=requested_environments,
    )
    rows_out: list[dict[str, object]] = []
    for row_result in result.rows:
        phases_out: dict[str, dict[str, object]] = {}
        for phase_name, phase in row_result.phases.items():
            entry: dict[str, object] = {
                "status": phase.status,
                "detail": phase.detail,
                "artifacts": [str(a) for a in phase.artifacts],
                "error_type": phase.error_type,
                "elapsed_seconds": (
                    round(phase.elapsed_seconds, 3) if phase.elapsed_seconds is not None else None
                ),
            }
            phases_out[phase_name] = entry
        rows_out.append(
            {
                "chart": row_result.row.chart,
                "env": row_result.row.env,
                "release": row_result.row.release,
                "namespace": row_result.row.namespace,
                "phases": phases_out,
            }
        )

    tally = result.tally()
    payload: dict[str, object] = {
        # The number a caller reading this document off `chart validate -o
        # json` would also see in `$?`. Both come from `exit_code_for` of the
        # *same* `RunResult.outcome()` fold, which is what stops the payload
        # and the process status from disagreeing about a run. The fold is
        # the service's; the number is `plumbing/exit_codes.py`'s.
        "exit_code": exit_code_for(result.outcome()),
        "rendered_root": str(result.rendered_root),
        "summary": {
            "rows": tally.rows,
            "passing_rows": tally.passing,
            "failing_rows": tally.failing,
            "spec_errors": len(result.spec_errors),
        },
        "rows": rows_out,
        "spec_errors": list(result.spec_errors),
    }
    if diagnostics:
        payload["diagnostics"] = diagnostics
    return payload


def _diagnostics(
    outcome: RunOutcome,
    *,
    requested_charts: tuple[str, ...],
    requested_environments: tuple[str, ...],
) -> dict[str, object]:
    """Project an outcome's planning metadata onto the JSON diagnostics object."""
    selection: dict[str, object] = {
        "requested_filters": {
            "charts": list(requested_charts),
            "environments": list(requested_environments),
        },
        "unmatched_filters": {
            "charts": list(outcome.unmatched_charts),
            "environments": list(outcome.unmatched_environments),
        },
        "ignored_changes": [str(path) for path in outcome.ignored_changes],
        "unmatched_changes": [str(path) for path in outcome.unmatched_changes],
        "rows_filtered_out": outcome.rows_filtered_out,
        "charts_unvalidated": outcome.charts_unvalidated,
    }
    has_selection_diagnostics = any(
        (
            requested_charts,
            requested_environments,
            outcome.unmatched_charts,
            outcome.unmatched_environments,
            outcome.ignored_changes,
            outcome.unmatched_changes,
            outcome.rows_filtered_out,
            outcome.charts_unvalidated,
        )
    )
    diagnostics: dict[str, object] = {}
    if outcome.warnings:
        diagnostics["warnings"] = list(outcome.warnings)
    if has_selection_diagnostics:
        diagnostics["selection"] = selection
    reason = no_work_reason(
        outcome,
        requested_charts=requested_charts,
        requested_environments=requested_environments,
    )
    if reason is not None:
        # Selection is present whenever there is a no-work reason, giving
        # consumers one stable object shape for explaining an empty run.
        diagnostics.setdefault("selection", selection)
        diagnostics["no_work_reason"] = reason
    return diagnostics
