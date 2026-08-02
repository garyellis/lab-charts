"""GitHub-flavored markdown projection of a validate run.

Suitable for `$GITHUB_STEP_SUMMARY`, a PR comment, or a Slack upload. This
was ~300 lines inside `wire.py` -- emoji tables, `<details>` blocks,
hand-rolled HTML escaping, fence-length computation -- which made that module
four times the size of every other `wire.py` in the tree and made it a
renderer wearing a contract's name. `wire.py` keeps the versioned JSON
payload; markdown is a *rendering* of the same `RunResult` and versions with
nothing.

It stays in `services/` rather than moving to `cli/` because
`ManifestValidationService.write_summaries` writes `summary.md`, and a
service may not import a surface.

Deliberately Rich-free and I/O-free, like its siblings: a worker process
rendering a PR comment has no terminal.
"""

from __future__ import annotations

from pathlib import Path

from chart_manager.services.manifest_validation.models import (
    PHASE_ORDER,
    PhaseResult,
    RowResult,
    RunOutcome,
    RunResult,
    no_work_reason,
    row_elapsed_text,
)

__all__ = ["to_markdown"]

_MD_STATUS_EMOJI = {
    "PASS": "✅",  # check mark
    "FAIL": "❌",  # cross mark
    "SKIP": "➖",  # noqa: RUF001 — heavy minus glyph chosen for markdown emoji symmetry
    "NOT_RUN": "·",  # middle dot
}


def to_markdown(
    source: RunResult | RunOutcome,
    *,
    include_timings: bool = False,
    requested_charts: tuple[str, ...] = (),
    requested_environments: tuple[str, ...] = (),
) -> str:
    """Render a run result or outcome as GitHub-flavored markdown.

    Always emits a heading + tally line so an empty result is still
    self-describing. Passing the full outcome preserves planning
    diagnostics; accepting a bare RunResult keeps the original projection
    API compatible.

    The outcome is threaded through as itself rather than flattened into a
    diagnostics dict first: every field the diagnostics section renders is
    already typed on `RunOutcome`, and the intermediate `dict[str, object]`
    only bought two `isinstance` re-checks of shapes this module had just
    built.
    """
    result = source.result if isinstance(source, RunOutcome) else source
    outcome = source if isinstance(source, RunOutcome) else None
    lines: list[str] = ["## validate", ""]

    if not result.rows:
        reason = (
            None
            if outcome is None
            else no_work_reason(
                outcome,
                requested_charts=requested_charts,
                requested_environments=requested_environments,
            )
        )
        lines.append(
            f"_nothing to validate: {reason}_" if reason else "_nothing to validate_"
        )
        diagnostic_lines = _markdown_diagnostics(
            outcome,
            requested_charts=requested_charts,
            requested_environments=requested_environments,
        )
        if diagnostic_lines:
            lines.extend(["", "### Diagnostics", "", *diagnostic_lines])
        warnings = _markdown_warnings(result, outcome)
        if warnings:
            # An outcome always carries a no-work reason on an empty run, so
            # "did a caller hand us diagnostics at all" is exactly "is this
            # an outcome" -- which decides whether the warnings need their
            # own heading to stay distinguishable from the section above.
            if outcome is not None:
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
    tally = result.tally()
    lines.append(
        f"**{tally.rows} rows · {tally.passing} passing · "
        f"{tally.failing} failing · {tally.skipped} skipped**"
    )

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
    diagnostic_lines = _markdown_diagnostics(
        outcome,
        requested_charts=requested_charts,
        requested_environments=requested_environments,
    )
    if diagnostic_lines:
        lines.extend(["### Diagnostics", "", *diagnostic_lines, ""])

    warnings = _markdown_warnings(result, outcome)
    if warnings:
        lines.extend(["### Warnings", ""])
        lines.extend(warnings)

    return "\n".join(lines).rstrip() + "\n"


def _md_cell(phase: PhaseResult | None) -> str:
    """Map a phase status to its markdown emoji cell."""
    if phase is None:
        return _MD_STATUS_EMOJI["NOT_RUN"]
    return _MD_STATUS_EMOJI.get(phase.status, phase.status)


def _markdown_failure_blocks(result: RunResult) -> list[list[str]]:
    """Collect a <details> block for every failed phase."""
    blocks: list[list[str]] = []
    for row_result in result.rows:
        for phase_name in _phase_iter(row_result):
            phase = row_result.phases[phase_name]
            if phase.status != "FAIL":
                continue
            blocks.append(
                _md_details_block(
                    summary=f"{row_result.row.chart}/{row_result.row.env} — {phase_name}",
                    detail=phase.detail or "",
                    artifacts=phase.artifacts,
                )
            )
    return blocks


def _markdown_advisory_blocks(result: RunResult) -> list[list[str]]:
    """Collect a <details> block for every PASS phase carrying advisory detail."""
    blocks: list[list[str]] = []
    for row_result in result.rows:
        for phase_name in _phase_iter(row_result):
            phase = row_result.phases[phase_name]
            if phase.status != "PASS" or not phase.detail:
                continue
            blocks.append(
                _md_details_block(
                    summary=f"{row_result.row.chart}/{row_result.row.env} — {phase_name}",
                    detail=phase.detail,
                    artifacts=phase.artifacts,
                )
            )
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
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


def _markdown_warnings(result: RunResult, outcome: RunOutcome | None) -> list[str]:
    """Render operator warnings and spec errors as markdown bullets."""
    out: list[str] = []
    if outcome is not None:
        out.extend(f"- {warning}" for warning in outcome.warnings)
    if result.spec_errors:
        out.append(f"- {len(result.spec_errors)} spec error(s):")
        for err in result.spec_errors:
            out.append(f"  - {err}")
    return out


def _markdown_diagnostics(
    outcome: RunOutcome | None,
    *,
    requested_charts: tuple[str, ...],
    requested_environments: tuple[str, ...],
) -> list[str]:
    """Render non-warning selection diagnostics as concise bullets."""
    if outcome is None:
        return []
    lines: list[str] = []
    if requested_charts:
        lines.append(f"- Requested charts: {', '.join(requested_charts)}")
    if requested_environments:
        lines.append(f"- Requested environments: {', '.join(requested_environments)}")
    if outcome.unmatched_charts:
        lines.append(f"- Unmatched charts: {', '.join(outcome.unmatched_charts)}")
    if outcome.unmatched_environments:
        lines.append(
            f"- Unmatched environments: {', '.join(outcome.unmatched_environments)}"
        )
    if outcome.ignored_changes:
        lines.append("- Ignored changes:")
        lines.extend(f"  - `{path}`" for path in outcome.ignored_changes)
    if outcome.unmatched_changes:
        lines.append("- Changes matching no trigger:")
        lines.extend(f"  - `{path}`" for path in outcome.unmatched_changes)
    if outcome.rows_filtered_out:
        lines.append(f"- Rows filtered out: {outcome.rows_filtered_out}")
    if outcome.charts_unvalidated:
        lines.append(
            f"- Charts without manifest-validation configuration: {outcome.charts_unvalidated}"
        )
    return lines


def _phase_iter(row_result: RowResult) -> list[str]:
    """Iterate phases in a stable order: render, schema, policy, then any extras."""
    phases = row_result.phases
    ordered = [p for p in PHASE_ORDER if p in phases]
    extras = sorted(p for p in phases if p not in PHASE_ORDER)
    return ordered + extras
