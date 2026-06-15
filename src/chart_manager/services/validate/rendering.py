"""Output renderers for RunResult.

Each renderer is a pure function over RunResult. CLI/CI integration
(stdout writes, $GITHUB_STEP_SUMMARY, file emission) lives in the CLI
layer so renderers stay easy to snapshot-test.
"""
from __future__ import annotations

from pathlib import Path

from rich.table import Table
from rich.text import Text

from chart_manager.plumbing.validate_models import PhaseResult, RunResult

# Stable, jq-friendly JSON shape. Bump on breaking changes only; additive
# fields are safe at this version.
JSON_SCHEMA_VERSION = 1

_MD_STATUS_EMOJI = {
    "PASS": "✅",  # check mark
    "FAIL": "❌",  # cross mark
    "SKIP": "➖",  # noqa: RUF001 — heavy minus glyph chosen for markdown emoji symmetry
    "NOT_RUN": "·",  # middle dot
}

_PHASE_ORDER: tuple[str, ...] = ("render", "schema", "policy")

_STATUS_STYLE = {
    "PASS": "green",
    "FAIL": "red",
    "SKIP": "dim",
    "NOT_RUN": "dim",
}


def to_text_table(result: RunResult, *, include_timings: bool = False) -> Table:
    columns = ["Chart", "Env", "Release", "Render", "Schema", "Policy"]
    if include_timings:
        columns.append("Elapsed")
    table = Table(*columns, title="validate")
    for row_result in result.rows:
        cells: list[str | Text] = [
            row_result.row.chart,
            row_result.row.env,
            row_result.row.release,
            _cell(row_result.phases.get("render")),
            _cell(row_result.phases.get("schema")),
            _cell(row_result.phases.get("policy")),
        ]
        if include_timings:
            cells.append(Text(_row_elapsed_text(row_result), style="dim"))
        table.add_row(*cells)
    return table


def _row_elapsed_text(row_result) -> str:
    total = 0.0
    any_timed = False
    for phase in row_result.phases.values():
        if phase.elapsed_seconds is not None:
            total += phase.elapsed_seconds
            any_timed = True
    return f"{total:.1f}s" if any_timed else ""


def failure_details(result: RunResult) -> list[str]:
    """One block per failed phase, suitable for printing under the table."""
    blocks: list[str] = []
    for row_result in result.rows:
        for phase_name, phase in row_result.phases.items():
            if phase.status != "FAIL":
                continue
            detail = phase.detail or ""
            header = (
                f"[red]{row_result.row.chart}/{row_result.row.env}[/red] "
                f"[bold]{phase_name}[/bold]"
            )
            artifacts = "\n".join(f"  artifact: {a}" for a in phase.artifacts)
            block = header + ("\n" + detail if detail else "")
            if artifacts:
                block += "\n" + artifacts
            blocks.append(block)
    return blocks


def advisory_details(result: RunResult) -> list[str]:
    """One block per PASS phase that carries advisory detail (e.g. kyverno warns)."""
    blocks: list[str] = []
    for row_result in result.rows:
        for phase_name, phase in row_result.phases.items():
            if phase.status != "PASS" or not phase.detail:
                continue
            header = (
                f"[yellow]{row_result.row.chart}/{row_result.row.env}[/yellow] "
                f"[bold]{phase_name}[/bold]"
            )
            blocks.append(header + "\n" + phase.detail)
    return blocks


def _cell(phase: PhaseResult | None) -> Text:
    if phase is None:
        return Text("-", style="dim")
    style = _STATUS_STYLE.get(phase.status, "")
    return Text(phase.status, style=style)


def to_markdown(result: RunResult, *, include_timings: bool = False) -> str:
    """Render a RunResult as GitHub-flavored markdown.

    Suitable for $GITHUB_STEP_SUMMARY and PR comments. Always emits a
    heading + tally line so an empty result is still self-describing.
    """
    lines: list[str] = ["## validate", ""]

    if not result.rows:
        lines.append("_nothing to validate_")
        warnings = _markdown_warnings(result)
        if warnings:
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
            cells.append(_row_elapsed_text(row_result))
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
    warnings = _markdown_warnings(result)
    if warnings:
        lines.extend(["### Warnings", ""])
        lines.extend(warnings)

    return "\n".join(lines).rstrip() + "\n"


def to_json(result: RunResult, *, include_timings: bool = False) -> dict[str, object]:
    """Render a RunResult as a stable, jq-friendly dict.

    Uses str(Path) for any path so json.dumps works without a custom
    encoder. `schema_version` is the breaking-change signal for
    downstream consumers; bump only on breaking change.

    `elapsed_seconds` is always present (null when the phase didn't run)
    so downstream tooling can rely on the key existing regardless of
    --timings. Rounded to ms so two runs of the same workload diff
    cleanly. `include_timings` is retained for parity with the text/md
    renderers but no longer affects JSON shape.
    """
    _ = include_timings  # JSON always emits; flag kept for renderer parity.
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

    return {
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


def _md_cell(phase: PhaseResult | None) -> str:
    if phase is None:
        return _MD_STATUS_EMOJI["NOT_RUN"]
    return _MD_STATUS_EMOJI.get(phase.status, phase.status)


def _markdown_tally(result: RunResult) -> str:
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


def _markdown_warnings(result: RunResult) -> list[str]:
    out: list[str] = []
    if result.spec_errors:
        out.append(f"- {len(result.spec_errors)} spec error(s):")
        for err in result.spec_errors:
            out.append(f"  - {err}")
    return out


def _phase_iter(phases) -> list[str]:
    """Iterate phases in a stable order: render, schema, policy, then any extras."""
    ordered = [p for p in _PHASE_ORDER if p in phases]
    extras = sorted(p for p in phases if p not in _PHASE_ORDER)
    return ordered + extras
