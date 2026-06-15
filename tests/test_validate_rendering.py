from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from rich.console import Console

from chart_manager.plumbing.validate_models import (
    PhaseResult,
    RowResult,
    RunResult,
    WorklistRow,
)
from chart_manager.services.validate.rendering import (
    JSON_SCHEMA_VERSION,
    advisory_details,
    failure_details,
    to_json,
    to_markdown,
    to_text_table,
)

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"
RUN_RESULT_GOLDEN = GOLDEN_DIR / "run-result.md.golden"


def _mixed_run_result() -> RunResult:
    """A RunResult covering PASS/FAIL/SKIP/NOT_RUN, with advisory + spec error."""
    return RunResult(
        rows=(
            RowResult(
                row=WorklistRow(chart="grafana", env="dev", release="grafana", namespace="lab-dev"),
                phases={
                    "render": PhaseResult(phase="render", status="PASS"),
                    "schema": PhaseResult(phase="schema", status="PASS"),
                    "policy": PhaseResult(
                        phase="policy",
                        status="PASS",
                        detail="warnings:\nrequire-non-root/audit: Pod/grafana: container runs as 0",
                    ),
                },
            ),
            RowResult(
                row=WorklistRow(
                    chart="schema-violator",
                    env="dev",
                    release="schema-violator",
                    namespace="lab-dev",
                ),
                phases={
                    "render": PhaseResult(phase="render", status="PASS"),
                    "schema": PhaseResult(
                        phase="schema",
                        status="FAIL",
                        detail=(
                            "Deployment/schema-violator "
                            "(/abs/path/Deployment.yaml): "
                            "at '/spec/replicas': got string, want null or integer"
                        ),
                        artifacts=(Path("/abs/path/Deployment.yaml"),),
                        error_type=None,
                    ),
                    "policy": PhaseResult(phase="policy", status="SKIP"),
                },
            ),
            RowResult(
                row=WorklistRow(chart="future-app", env="dev", release="future-app", namespace="lab-dev"),
                phases={
                    "render": PhaseResult(phase="render", status="NOT_RUN"),
                    "schema": PhaseResult(phase="schema", status="NOT_RUN"),
                    "policy": PhaseResult(phase="policy", status="NOT_RUN"),
                },
            ),
        ),
        rendered_root=Path("/abs/render-root"),
        spec_errors=("charts/broken/validate-spec.yaml: unknown major version 99",),
    )


def _render_table(result: RunResult) -> str:
    console = Console(record=True, width=120)
    console.print(to_text_table(result))
    return console.export_text()


def test_table_has_expected_columns_and_status_text() -> None:
    result = RunResult(
        rows=(
            RowResult(
                row=WorklistRow(chart="grafana", env="dev", release="grafana", namespace="lab-dev"),
                phases={
                    "render": PhaseResult(phase="render", status="PASS"),
                    "schema": PhaseResult(phase="schema", status="NOT_RUN"),
                    "policy": PhaseResult(phase="policy", status="NOT_RUN"),
                },
            ),
        ),
        rendered_root=Path("/tmp/x"),
    )
    text = _render_table(result)
    for column in ("Chart", "Env", "Release", "Render", "Schema", "Policy"):
        assert column in text
    assert "grafana" in text
    assert "dev" in text
    assert "PASS" in text
    assert "NOT_RUN" in text


def test_failure_details_renders_tool_error_cleanly() -> None:
    result = RunResult(
        rows=(
            RowResult(
                row=WorklistRow(chart="bad", env="dev", release="bad", namespace="lab-dev"),
                phases={
                    "render": PhaseResult(
                        phase="render",
                        status="FAIL",
                        detail="helm template failed at /tmp/render",
                        artifacts=(Path("/tmp/render"),),
                        error_type="tool",
                    ),
                    "schema": PhaseResult(phase="schema", status="NOT_RUN"),
                    "policy": PhaseResult(phase="policy", status="NOT_RUN"),
                },
            ),
        ),
        rendered_root=Path("/tmp/render"),
    )
    blocks = failure_details(result)
    assert len(blocks) == 1
    block = blocks[0]
    assert "bad/dev" in block
    assert "render" in block
    assert "helm template failed at /tmp/render" in block
    assert "/tmp/render" in block


def test_failure_details_empty_when_no_fail() -> None:
    result = RunResult(
        rows=(
            RowResult(
                row=WorklistRow(chart="ok", env="dev", release="ok", namespace="lab-dev"),
                phases={
                    "render": PhaseResult(phase="render", status="PASS"),
                },
            ),
        ),
        rendered_root=Path("/tmp/x"),
    )
    assert failure_details(result) == []


def test_advisory_details_surfaces_pass_phase_with_detail() -> None:
    result = RunResult(
        rows=(
            RowResult(
                row=WorklistRow(chart="advisor", env="dev", release="advisor", namespace="lab-dev"),
                phases={
                    "render": PhaseResult(phase="render", status="PASS"),
                    "schema": PhaseResult(phase="schema", status="PASS"),
                    "policy": PhaseResult(
                        phase="policy",
                        status="PASS",
                        detail="warnings:\nadvisory/r: Pod/p: heads up",
                    ),
                },
            ),
        ),
        rendered_root=Path("/tmp/x"),
    )
    blocks = advisory_details(result)
    assert len(blocks) == 1
    assert "advisor/dev" in blocks[0]
    assert "policy" in blocks[0]
    assert "heads up" in blocks[0]


def test_advisory_details_empty_when_no_pass_detail() -> None:
    result = RunResult(
        rows=(
            RowResult(
                row=WorklistRow(chart="clean", env="dev", release="clean", namespace="lab-dev"),
                phases={
                    "render": PhaseResult(phase="render", status="PASS"),
                    "policy": PhaseResult(phase="policy", status="PASS"),
                },
            ),
        ),
        rendered_root=Path("/tmp/x"),
    )
    assert advisory_details(result) == []


# ---- to_markdown -----------------------------------------------------------


def test_to_markdown_matches_golden() -> None:
    """Snapshot test against a hand-curated mixed RunResult.

    Regenerate with: REGEN_GOLDEN=1 uv run --extra dev pytest tests/test_validate_rendering.py
    """
    actual = to_markdown(_mixed_run_result())
    if os.environ.get("REGEN_GOLDEN"):
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        RUN_RESULT_GOLDEN.write_text(actual)
        pytest.fail(f"regenerated golden at {RUN_RESULT_GOLDEN}")
    expected = RUN_RESULT_GOLDEN.read_text()
    assert actual == expected


def test_to_markdown_empty_result_is_self_describing() -> None:
    md = to_markdown(RunResult(rows=(), rendered_root=Path("/tmp/x")))
    assert md.startswith("## validate")
    assert "nothing to validate" in md
    assert "### Failures" not in md
    assert "### Advisories" not in md


def test_to_markdown_no_failures_section_when_all_pass() -> None:
    result = RunResult(
        rows=(
            RowResult(
                row=WorklistRow(chart="ok", env="dev", release="ok", namespace="lab-dev"),
                phases={
                    "render": PhaseResult(phase="render", status="PASS"),
                    "schema": PhaseResult(phase="schema", status="PASS"),
                    "policy": PhaseResult(phase="policy", status="PASS"),
                },
            ),
        ),
        rendered_root=Path("/tmp/x"),
    )
    md = to_markdown(result)
    assert "### Failures" not in md
    assert "### Advisories" not in md
    # Table still rendered.
    assert "| ok | dev |" in md


def test_to_markdown_no_advisories_section_when_pass_without_detail() -> None:
    result = RunResult(
        rows=(
            RowResult(
                row=WorklistRow(chart="bad", env="dev", release="bad", namespace="lab-dev"),
                phases={
                    "render": PhaseResult(
                        phase="render",
                        status="FAIL",
                        detail="boom",
                        artifacts=(Path("/tmp/x"),),
                        error_type="tool",
                    ),
                },
            ),
        ),
        rendered_root=Path("/tmp/x"),
    )
    md = to_markdown(result)
    assert "### Failures" in md
    assert "### Advisories" not in md


# ---- to_json ---------------------------------------------------------------


def test_to_json_shape_and_schema_version() -> None:
    data = to_json(_mixed_run_result())
    assert data["schema_version"] == JSON_SCHEMA_VERSION == 1
    assert isinstance(data["exit_code"], int)
    # spec_errors present => exit_code == 3
    assert data["exit_code"] == 3
    assert data["rendered_root"] == "/abs/render-root"

    summary = data["summary"]
    for key in ("rows", "passing_rows", "failing_rows", "spec_errors"):
        assert key in summary
        assert isinstance(summary[key], int)
    assert summary["rows"] == 3
    assert summary["spec_errors"] == 1

    assert len(data["rows"]) == 3
    grafana = data["rows"][0]
    assert grafana["chart"] == "grafana"
    assert grafana["namespace"] == "lab-dev"
    # Each phase is a dict with stable keys. elapsed_seconds is always
    # present (null when not measured) so downstream tooling can rely on it.
    for phase_name in ("render", "schema", "policy"):
        phase = grafana["phases"][phase_name]
        assert set(phase.keys()) == {
            "status",
            "detail",
            "artifacts",
            "error_type",
            "elapsed_seconds",
        }
        assert isinstance(phase["artifacts"], list)
        # Fixture doesn't set elapsed_seconds, so they must be null (not absent).
        assert phase["elapsed_seconds"] is None

    # Path values stringified, not Path objects.
    schema_violator = data["rows"][1]
    artifacts = schema_violator["phases"]["schema"]["artifacts"]
    assert artifacts == ["/abs/path/Deployment.yaml"]
    for a in artifacts:
        assert isinstance(a, str)

    assert data["spec_errors"] == [
        "charts/broken/validate-spec.yaml: unknown major version 99"
    ]


def test_to_json_round_trips_through_json_module() -> None:
    data = to_json(_mixed_run_result())
    payload = json.dumps(data, indent=2)
    parsed = json.loads(payload)
    assert parsed == data


# ---- markdown safety -------------------------------------------------------


def test_to_markdown_picks_longer_fence_when_detail_contains_backticks() -> None:
    """A kyverno/helm detail with its own ``` must not terminate our fence."""
    detail_with_fence = "before\n```\nembedded\n```\nafter"
    result = RunResult(
        rows=(
            RowResult(
                row=WorklistRow(chart="c", env="dev", release="c", namespace="lab-dev"),
                phases={
                    "render": PhaseResult(
                        phase="render",
                        status="FAIL",
                        detail=detail_with_fence,
                        artifacts=(),
                        error_type="tool",
                    ),
                },
            ),
        ),
        rendered_root=Path("/tmp/x"),
    )
    md = to_markdown(result)
    # Outer fence must be at least 4 backticks since the body contains a 3-backtick run.
    assert "````" in md
    # The embedded ``` is preserved verbatim — not stripped, not escaped away.
    assert "embedded" in md
    # The outer ```` fences appear in matched pairs (open + close).
    assert md.count("````") >= 2


def test_to_markdown_escapes_html_in_summary() -> None:
    """A chart/env name containing < > & must not corrupt the <summary> tag."""
    result = RunResult(
        rows=(
            RowResult(
                row=WorklistRow(
                    chart="weird<name>",
                    env="d&v",
                    release="r",
                    namespace="lab-dev",
                ),
                phases={
                    "render": PhaseResult(
                        phase="render",
                        status="FAIL",
                        detail="boom",
                        artifacts=(),
                        error_type="tool",
                    ),
                },
            ),
        ),
        rendered_root=Path("/tmp/x"),
    )
    md = to_markdown(result)
    # Raw HTML-sensitive characters must not appear inside the summary text.
    assert "<summary>weird<name>" not in md
    assert "&lt;name&gt;" in md
    assert "d&amp;v" in md
