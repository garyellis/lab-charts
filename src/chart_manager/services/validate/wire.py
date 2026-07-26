"""Versioned wire contract for `validate` results.

This module is the single source of truth for the machine-readable
projections of a `RunResult`: the jq-friendly JSON payload and the
GitHub-flavored markdown summary. Every surface -- the CLI's `--format
json|md`, a REST endpoint, a PR-comment bot, a Slack app -- projects through
these functions so they cannot diverge while all claiming the same
`JSON_SCHEMA_VERSION`.

**Editing this module is a breaking change.** Adding a key is additive and
safe at the current version; renaming, removing, or retyping a key requires
bumping `JSON_SCHEMA_VERSION`.

Deliberately Rich-free and I/O-free. Nothing here may import `rich`: an HTTP
server has no terminal, and `to_json` must not drag a TUI library into a
worker process. Terminal rendering (Rich tables, color styles, console
markup) lives in `cli/validate_render.py`; a test in
`tests/test_validate_rendering.py` asserts that importing this module leaves
`rich` out of `sys.modules`.
"""
from __future__ import annotations

from pathlib import Path

from chart_manager.services.validate.domain.models import (
    PHASE_ORDER,
    PhaseResult,
    RunResult,
)
from chart_manager.services.validate.requests import RunOutcome

# Stable, jq-friendly JSON shape. Bump on breaking changes only; additive
# fields are safe at this version.
JSON_SCHEMA_VERSION = 1

_MD_STATUS_EMOJI = {
    "PASS": "✅",  # check mark
    "FAIL": "❌",  # cross mark
    "SKIP": "➖",  # noqa: RUF001 — heavy minus glyph chosen for markdown emoji symmetry
    "NOT_RUN": "·",  # middle dot
}

__all__ = [
    "JSON_SCHEMA_VERSION",
    "row_elapsed_text",
    "to_json",
    "to_markdown",
]


def row_elapsed_text(row_result) -> str:
    """Sum the row's phase timings; empty string when nothing was timed.

    Shared by the markdown table and the terminal table so the "Elapsed"
    column reads identically in both.
    """
    total = 0.0
    any_timed = False
    for phase in row_result.phases.values():
        if phase.elapsed_seconds is not None:
            total += phase.elapsed_seconds
            any_timed = True
    return f"{total:.1f}s" if any_timed else ""


def to_markdown(
    source: RunResult | RunOutcome,
    *,
    include_timings: bool = False,
    requested_charts: tuple[str, ...] = (),
    requested_environments: tuple[str, ...] = (),
) -> str:
    """Render a run result or outcome as GitHub-flavored markdown.

    Suitable for $GITHUB_STEP_SUMMARY and PR comments. Always emits a
    heading + tally line so an empty result is still self-describing.
    Passing the full outcome preserves planning diagnostics; accepting a
    bare RunResult keeps the original projection API compatible.
    """
    result, diagnostics = _wire_inputs(
        source,
        requested_charts=requested_charts,
        requested_environments=requested_environments,
    )
    lines: list[str] = ["## validate", ""]

    if not result.rows:
        no_work_reason = diagnostics.get("no_work_reason")
        lines.append(
            f"_nothing to validate: {no_work_reason}_"
            if no_work_reason
            else "_nothing to validate_"
        )
        diagnostic_lines = _markdown_diagnostics(diagnostics)
        if diagnostic_lines:
            lines.extend(["", "### Diagnostics", "", *diagnostic_lines])
        warnings = _markdown_warnings(result, diagnostics)
        if warnings:
            if diagnostics:
                lines.extend(["", "### Warnings", "", *warnings])
            else:
                lines.extend(["", *warnings])
        return "\n".join(lines).rstrip() + "\n"

    # Status table.
    header = ["Chart", "Env", "Release", "Render", "Schema", "Policy"]
    if include_timings:
        header.append("Elapsed")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for row_result in result.rows:
        cells = [
            row_result.row.chart,
            row_result.row.env,
            row_result.row.release,
            _md_cell(row_result.phases.get("render")),
            _md_cell(row_result.phases.get("schema")),
            _md_cell(row_result.phases.get("policy")),
        ]
        if include_timings:
            cells.append(row_elapsed_text(row_result))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    # One-line tally.
    lines.append(_markdown_tally(result))

    # Failures section — only when there are failures.
    failure_blocks = _markdown_failure_blocks(result)
    if failure_blocks:
        lines.extend(["", "### Failures", ""])
        for block in failure_blocks:
            lines.extend(block)
            lines.append("")

    # Advisories — only when present.
    advisory_blocks = _markdown_advisory_blocks(result)
    if advisory_blocks:
        lines.extend(["### Advisories", ""])
        for block in advisory_blocks:
            lines.extend(block)
            lines.append("")

    # Warnings (spec errors, etc.) — only when present.
    diagnostic_lines = _markdown_diagnostics(diagnostics)
    if diagnostic_lines:
        lines.extend(["### Diagnostics", "", *diagnostic_lines, ""])

    warnings = _markdown_warnings(result, diagnostics)
    if warnings:
        lines.extend(["### Warnings", ""])
        lines.extend(warnings)

    return "\n".join(lines).rstrip() + "\n"


def to_json(
    source: RunResult | RunOutcome,
    *,
    requested_charts: tuple[str, ...] = (),
    requested_environments: tuple[str, ...] = (),
) -> dict[str, object]:
    """Render a run result or outcome as a stable, jq-friendly dict.

    Uses str(Path) for any path so json.dumps works without a custom
    encoder. `schema_version` is the breaking-change signal for
    downstream consumers; bump only on breaking change.

    `elapsed_seconds` is always present (null when the phase didn't run)
    so downstream tooling can rely on the key existing regardless of
    --timings. Rounded to ms so two runs of the same workload diff
    cleanly. There is deliberately no `include_timings` switch: JSON
    always emits them, and a no-op flag on a versioned wire contract
    invites a consumer to depend on it.
    """
    result, diagnostics = _wire_inputs(
        source,
        requested_charts=requested_charts,
        requested_environments=requested_environments,
    )
    rows_out: list[dict[str, object]] = []
    passing_rows = 0
    failing_rows = 0
    for row_result in result.rows:
        phases_out: dict[str, dict[str, object]] = {}
        for phase_name, phase in row_result.phases.items():
            entry: dict[str, object] = {
                "status": phase.status,
                "detail": phase.detail,
                "artifacts": [str(a) for a in phase.artifacts],
                "error_type": phase.error_type,
                "elapsed_seconds": (
                    round(phase.elapsed_seconds, 3)
                    if phase.elapsed_seconds is not None
                    else None
                ),
            }
            phases_out[phase_name] = entry
        statuses = {p.status for p in row_result.phases.values()}
        if "FAIL" in statuses:
            failing_rows += 1
        elif statuses and statuses <= {"PASS", "SKIP", "NOT_RUN"} and "PASS" in statuses:
            passing_rows += 1
        rows_out.append({
            "chart": row_result.row.chart,
            "env": row_result.row.env,
            "release": row_result.row.release,
            "namespace": row_result.row.namespace,
            "phases": phases_out,
        })

    payload: dict[str, object] = {
        "schema_version": JSON_SCHEMA_VERSION,
        "exit_code": result.exit_code(),
        "rendered_root": str(result.rendered_root),
        "summary": {
            "rows": len(result.rows),
            "passing_rows": passing_rows,
            "failing_rows": failing_rows,
            "spec_errors": len(result.spec_errors),
        },
        "rows": rows_out,
        "spec_errors": list(result.spec_errors),
    }
    # Preserve byte-for-byte compatibility for callers that still project a
    # bare RunResult. The object is additive when the richer RunOutcome
    # carries planning diagnostics.
    if diagnostics:
        payload["diagnostics"] = diagnostics
    return payload


def _md_cell(phase: PhaseResult | None) -> str:
    """Map a phase status to its markdown emoji cell."""
    if phase is None:
        return _MD_STATUS_EMOJI["NOT_RUN"]
    return _MD_STATUS_EMOJI.get(phase.status, phase.status)


def _markdown_tally(result: RunResult) -> str:
    """Build the bold one-line tally; any FAIL makes a row failing."""
    n_rows = len(result.rows)
    passing = 0
    failing = 0
    skipped = 0
    for row_result in result.rows:
        statuses = {p.status for p in row_result.phases.values()}
        if "FAIL" in statuses:
            failing += 1
        elif statuses and statuses <= {"SKIP", "NOT_RUN"}:
            skipped += 1
        elif "PASS" in statuses:
            passing += 1
    return f"**{n_rows} rows · {passing} passing · {failing} failing · {skipped} skipped**"


def _markdown_failure_blocks(result: RunResult) -> list[list[str]]:
    """Collect a <details> block for every failed phase."""
    blocks: list[list[str]] = []
    for row_result in result.rows:
        for phase_name in _phase_iter(row_result.phases):
            phase = row_result.phases[phase_name]
            if phase.status != "FAIL":
                continue
            blocks.append(_md_details_block(
                summary=f"{row_result.row.chart}/{row_result.row.env} — {phase_name}",
                detail=phase.detail or "",
                artifacts=phase.artifacts,
            ))
    return blocks


def _markdown_advisory_blocks(result: RunResult) -> list[list[str]]:
    """Collect a <details> block for every PASS phase carrying advisory detail."""
    blocks: list[list[str]] = []
    for row_result in result.rows:
        for phase_name in _phase_iter(row_result.phases):
            phase = row_result.phases[phase_name]
            if phase.status != "PASS" or not phase.detail:
                continue
            blocks.append(_md_details_block(
                summary=f"{row_result.row.chart}/{row_result.row.env} — {phase_name}",
                detail=phase.detail,
                artifacts=phase.artifacts,
            ))
    return blocks


def _md_details_block(*, summary: str, detail: str, artifacts: tuple[Path, ...]) -> list[str]:
    """Build a <details>...</details> block with fenced detail + artifact list.

    Defends against two markdown-breakage modes:
      * `summary` is interpolated into raw HTML — escape `<`, `>`, `&` so a
        chart/env name with HTML-sensitive characters cannot corrupt the
        surrounding <details><summary> tag.
      * `detail` is wrapped in a fenced code block — kyverno/helm output may
        itself contain ``` fences; pick a fence longer than the longest run
        of backticks in the body so the block terminates correctly.
    """
    safe_summary = _html_escape(summary)
    block = [f"<details><summary>{safe_summary}</summary>", ""]
    if detail:
        fence = _safe_fence(detail)
        block.extend([fence, detail.rstrip(), fence, ""])
    if artifacts:
        block.append("Artifacts:")
        for art in artifacts:
            block.append(f"- `{art}`")
        block.append("")
    block.append("</details>")
    return block


def _html_escape(value: str) -> str:
    """Escape &, <, > for safe interpolation into raw HTML."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _safe_fence(body: str) -> str:
    """Pick a backtick fence longer than any run of backticks in `body`."""
    longest = 0
    run = 0
    for ch in body:
        if ch == "`":
            run += 1
            if run > longest:
                longest = run
        else:
            run = 0
    return "`" * max(3, longest + 1)


def _markdown_warnings(
    result: RunResult, diagnostics: dict[str, object] | None = None
) -> list[str]:
    """Render operator warnings and spec errors as markdown bullets."""
    out: list[str] = []
    if diagnostics:
        warnings = diagnostics.get("warnings")
        if isinstance(warnings, list):
            out.extend(f"- {warning}" for warning in warnings)
    if result.spec_errors:
        out.append(f"- {len(result.spec_errors)} spec error(s):")
        for err in result.spec_errors:
            out.append(f"  - {err}")
    return out


def _markdown_diagnostics(diagnostics: dict[str, object]) -> list[str]:
    """Render non-warning selection diagnostics as concise bullets."""
    selection = diagnostics.get("selection")
    if not isinstance(selection, dict):
        return []
    lines: list[str] = []
    requested = selection["requested_filters"]
    unmatched = selection["unmatched_filters"]
    assert isinstance(requested, dict)
    assert isinstance(unmatched, dict)
    if requested["charts"]:
        lines.append(f"- Requested charts: {', '.join(requested['charts'])}")
    if requested["environments"]:
        lines.append(
            f"- Requested environments: {', '.join(requested['environments'])}"
        )
    if unmatched["charts"]:
        lines.append(f"- Unmatched charts: {', '.join(unmatched['charts'])}")
    if unmatched["environments"]:
        lines.append(
            f"- Unmatched environments: {', '.join(unmatched['environments'])}"
        )
    ignored = selection["ignored_changes"]
    if ignored:
        lines.append("- Ignored changes:")
        lines.extend(f"  - `{path}`" for path in ignored)
    unmatched_changes = selection["unmatched_changes"]
    if unmatched_changes:
        lines.append("- Changes matching no trigger:")
        lines.extend(f"  - `{path}`" for path in unmatched_changes)
    if selection["rows_filtered_out"]:
        lines.append(f"- Rows filtered out: {selection['rows_filtered_out']}")
    if selection["charts_unvalidated"]:
        lines.append(f"- Charts without a validation spec: {selection['charts_unvalidated']}")
    return lines


def _wire_inputs(
    source: RunResult | RunOutcome,
    *,
    requested_charts: tuple[str, ...],
    requested_environments: tuple[str, ...],
) -> tuple[RunResult, dict[str, object]]:
    """Normalize the backwards-compatible result/outcome projection inputs."""
    if isinstance(source, RunResult):
        return source, {}

    result = source.result
    no_work_reason: str | None = None
    if not result.rows:
        if source.unmatched_charts or source.unmatched_environments:
            no_work_reason = "requested filters did not match"
        elif requested_charts or requested_environments:
            no_work_reason = "requested filters selected no affected validation cases"
        elif source.unmatched_changes:
            no_work_reason = "changed files matched no validation trigger"
        elif source.ignored_changes:
            no_work_reason = "all relevant changed files were explicitly ignored"
        elif source.charts_unvalidated:
            no_work_reason = "no chart with a validation specification was selected"
        else:
            no_work_reason = "no affected validation cases"

    selection: dict[str, object] = {
        "requested_filters": {
            "charts": list(requested_charts),
            "environments": list(requested_environments),
        },
        "unmatched_filters": {
            "charts": list(source.unmatched_charts),
            "environments": list(source.unmatched_environments),
        },
        "ignored_changes": [str(path) for path in source.ignored_changes],
        "unmatched_changes": [str(path) for path in source.unmatched_changes],
        "rows_filtered_out": source.rows_filtered_out,
        "charts_unvalidated": source.charts_unvalidated,
    }
    has_selection_diagnostics = any((
        requested_charts,
        requested_environments,
        source.unmatched_charts,
        source.unmatched_environments,
        source.ignored_changes,
        source.unmatched_changes,
        source.rows_filtered_out,
        source.charts_unvalidated,
    ))
    diagnostics: dict[str, object] = {}
    if source.warnings:
        diagnostics["warnings"] = list(source.warnings)
    if has_selection_diagnostics:
        diagnostics["selection"] = selection
    if no_work_reason is not None:
        # Selection is present whenever there is a no-work reason, giving
        # consumers one stable object shape for explaining an empty run.
        diagnostics.setdefault("selection", selection)
        diagnostics["no_work_reason"] = no_work_reason
    return result, diagnostics


def _phase_iter(phases) -> list[str]:
    """Iterate phases in a stable order: render, schema, policy, then any extras."""
    ordered = [p for p in PHASE_ORDER if p in phases]
    extras = sorted(p for p in phases if p not in PHASE_ORDER)
    return ordered + extras
