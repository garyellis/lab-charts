"""CLI-layer tests for the validate sub-app.

We don't shell out to helm/kubeconform/kyverno here — that's integration
territory. These tests exercise the CLI's emission, format routing,
side-file writing, and GITHUB_STEP_SUMMARY behavior by driving the
internal `_emit_result` helper with a fabricated RunResult and by
invoking `--help` / `--format unknown` through Typer's CliRunner.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from chart_manager.cli import validate as validate_cli
from chart_manager.cli.main import app
from chart_manager.plumbing.errors import ChartNotFoundError
from chart_manager.services.manifest_validation.app import RunOutcome, ValidateInputError
from chart_manager.services.manifest_validation.models import (
    PhaseResult,
    RowResult,
    RunResult,
    WorklistRow,
)
from chart_manager.settings import DEFAULT_CHARTS_DIR


def _result() -> RunResult:
    return RunResult(
        rows=(
            RowResult(
                row=WorklistRow(chart="grafana", env="dev", release="grafana", namespace="lab-dev"),
                phases={
                    "render": PhaseResult(phase="render", status="PASS"),
                    "schema": PhaseResult(phase="schema", status="PASS"),
                    "policy": PhaseResult(phase="policy", status="PASS"),
                },
            ),
        ),
        rendered_root=Path("/tmp/x"),
    )


def _capture_stdout(fn) -> str:
    """Capture validate_cli.console output AND raw sys.stdout writes."""
    buf = io.StringIO()
    # Replace the module-level Rich console with one writing to our buffer.
    from rich.console import Console as _Console

    new_console = _Console(file=buf, force_terminal=False, no_color=True, width=200)
    old_console = validate_cli.console
    validate_cli.console = new_console
    try:
        with patch("sys.stdout", buf):
            fn()
    finally:
        validate_cli.console = old_console
    return buf.getvalue()


def test_record_lifecycle_evidence_records_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[RunOutcome] = []

    class Recorder:
        def __init__(self, root: Path, *, charts_dir: Path) -> None:
            assert root == tmp_path
            assert charts_dir == DEFAULT_CHARTS_DIR

        def record(self, outcome: RunOutcome):  # type: ignore[no-untyped-def]
            recorded.append(outcome)
            return type("Recording", (), {"diagnostics": ()})()

    monkeypatch.setattr(validate_cli, "ManifestValidationEvidenceRecorder", Recorder)
    outcome = RunOutcome(result=_result(), out_dir=tmp_path / "out")

    validate_cli._record_lifecycle_evidence(tmp_path, outcome)

    assert recorded == [outcome]


def test_record_lifecycle_evidence_is_nonfatal_and_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Recorder:
        def __init__(self, root: Path, *, charts_dir: Path) -> None:
            assert charts_dir == DEFAULT_CHARTS_DIR

        def record(self, outcome: RunOutcome):  # type: ignore[no-untyped-def]
            raise OSError("read-only state directory")

    monkeypatch.setattr(validate_cli, "ManifestValidationEvidenceRecorder", Recorder)
    outcome = RunOutcome(result=_result(), out_dir=tmp_path / "out")

    output = _capture_stdout(
        lambda: validate_cli._record_lifecycle_evidence(tmp_path, outcome)
    )

    assert "lifecycle evidence was not recorded" in output
    assert "read-only state directory" in output


def test_emit_json_writes_valid_json_with_schema_version(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    output = _capture_stdout(
        lambda: validate_cli._emit_result(_result(), fmt="json", out_dir=out_dir)
    )
    payload = json.loads(output)
    assert payload["schema_version"] == 1
    assert payload["exit_code"] == 0
    assert payload["summary"]["rows"] == 1
    # JSON format must not write summary.md
    assert not (out_dir / "summary.md").exists()


def test_emit_md_writes_markdown_starting_with_heading(tmp_path: Path) -> None:
    output = _capture_stdout(
        lambda: validate_cli._emit_result(_result(), fmt="md", out_dir=tmp_path / "out")
    )
    assert output.startswith("## validate")
    assert "| Chart |" in output
    # No Rich text-table glyphs in md mode.
    assert "PASS" not in output  # md uses ✅ not PASS


def test_emit_text_prints_table_and_does_not_emit_summary_md(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    output = _capture_stdout(
        lambda: validate_cli._emit_result(_result(), fmt="text", out_dir=out_dir)
    )
    assert "PASS" in output  # text-table cell text
    assert "Chart" in output
    assert not (out_dir / "summary.md").exists()
    assert not (out_dir / "summary.json").exists()


def test_emit_all_prints_text_and_writes_summary_md_and_json(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    output = _capture_stdout(
        lambda: validate_cli._emit_result(_result(), fmt="all", out_dir=out_dir)
    )
    # Text table on stdout.
    assert "PASS" in output
    # And summary.md exists with markdown contents.
    summary_md = out_dir / "summary.md"
    assert summary_md.is_file()
    md_contents = summary_md.read_text()
    assert md_contents.startswith("## validate")
    assert "| grafana |" in md_contents
    # And summary.json sidecar exists with structured contents.
    summary_json = out_dir / "summary.json"
    assert summary_json.is_file()
    payload = json.loads(summary_json.read_text())
    assert payload["schema_version"] == 1
    assert payload["summary"]["rows"] == 1


def test_github_step_summary_written_when_flag_passed_and_env_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    step_summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    _capture_stdout(
        lambda: validate_cli._emit_result(
            _result(),
            fmt="text",
            out_dir=tmp_path / "out",
            github_step_summary=True,
        )
    )
    assert step_summary.is_file()
    assert step_summary.read_text().startswith("## validate")


def test_github_step_summary_not_written_when_flag_not_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in semantics: env var alone must not trigger a write.

    Asserts the removal of the previous auto-detect behavior — without
    the explicit `--github-step-summary` flag the CLI must ignore the
    env var entirely, so running locally on a runner-like shell never
    surprise-writes to it.
    """
    step_summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    _capture_stdout(
        lambda: validate_cli._emit_result(
            _result(),
            fmt="text",
            out_dir=tmp_path / "out",
            # github_step_summary defaults to False
        )
    )
    assert not step_summary.exists()


def test_github_step_summary_warns_when_flag_passed_but_env_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag asserts intent; missing env var should warn but not crash."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    output = _capture_stdout(
        lambda: validate_cli._emit_result(
            _result(),
            fmt="text",
            out_dir=tmp_path / "out",
            github_step_summary=True,
        )
    )
    assert "GITHUB_STEP_SUMMARY" in output
    assert "not set" in output


def test_github_step_summary_appends_across_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub aggregates step summaries — verify append mode, not truncate."""
    step_summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    _capture_stdout(
        lambda: validate_cli._emit_result(
            _result(),
            fmt="text",
            out_dir=tmp_path / "out",
            github_step_summary=True,
        )
    )
    first_len = step_summary.stat().st_size
    _capture_stdout(
        lambda: validate_cli._emit_result(
            _result(),
            fmt="text",
            out_dir=tmp_path / "out",
            github_step_summary=True,
        )
    )
    assert step_summary.stat().st_size > first_len


def test_github_step_summary_unwritable_does_not_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Point at a path under a non-existent parent dir we can't create
    # because we use a regular file as the parent.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    bad_target = blocker / "step-summary.md"  # parent is a file, not a dir
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(bad_target))
    # Must not raise.
    output = _capture_stdout(
        lambda: validate_cli._emit_result(
            _result(),
            fmt="text",
            out_dir=tmp_path / "out",
            github_step_summary=True,
        )
    )
    assert "could not write GITHUB_STEP_SUMMARY" in output


@pytest.mark.parametrize("subcommand", ["render", "schema", "policy", "run"])
def test_each_subcommand_help_lists_github_step_summary_flag(subcommand: str) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["validate", subcommand, "--help"])
    assert result.exit_code == 0
    assert "--github-step-summary" in result.output


def test_validate_format_rejects_unknown_value() -> None:
    with pytest.raises(typer.BadParameter) as exc:
        validate_cli._validate_format("yaml")
    assert "yaml" in str(exc.value)
    assert "text" in str(exc.value)  # lists allowed values


def test_run_help_lists_format_option() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["validate", "run", "--help"])
    assert result.exit_code == 0
    assert "--format" in result.output


@pytest.mark.parametrize("subcommand", ["render", "schema", "policy", "run"])
def test_each_subcommand_help_lists_format_option(subcommand: str) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["validate", subcommand, "--help"])
    assert result.exit_code == 0
    assert "--format" in result.output


def test_emit_json_includes_elapsed_seconds_when_timings_set(tmp_path: Path) -> None:
    from chart_manager.services.manifest_validation.models import (
        PhaseResult,
        RowResult,
        RunResult,
        WorklistRow,
    )

    result = RunResult(
        rows=(
            RowResult(
                row=WorklistRow(chart="g", env="d", release="g", namespace="lab-d"),
                phases={
                    "render": PhaseResult(phase="render", status="PASS", elapsed_seconds=1.5),
                    "schema": PhaseResult(phase="schema", status="PASS", elapsed_seconds=0.2),
                    "policy": PhaseResult(phase="policy", status="PASS", elapsed_seconds=0.1),
                },
            ),
        ),
        rendered_root=Path("/tmp/x"),
    )
    out = _capture_stdout(
        lambda: validate_cli._emit_result(
            result, fmt="json", out_dir=tmp_path / "out", timings=True
        )
    )
    payload = json.loads(out)
    assert payload["rows"][0]["phases"]["render"]["elapsed_seconds"] == 1.5
    assert payload["schema_version"] == 1  # additive, no bump


def test_emit_json_always_emits_elapsed_seconds_key_null_when_unmeasured(tmp_path: Path) -> None:
    # JSON contract: elapsed_seconds is always present so downstream tooling
    # can rely on the key. null when --timings is off or the phase didn't
    # record one.
    out = _capture_stdout(
        lambda: validate_cli._emit_result(_result(), fmt="json", out_dir=tmp_path / "out")
    )
    payload = json.loads(out)
    render = payload["rows"][0]["phases"]["render"]
    assert "elapsed_seconds" in render
    assert render["elapsed_seconds"] is None


def test_emit_json_projects_outcome_and_requested_filter_diagnostics(
    tmp_path: Path,
) -> None:
    result = RunResult(rows=(), rendered_root=tmp_path / "out")
    outcome = RunOutcome(
        result=result,
        out_dir=result.rendered_root,
        warnings=("nothing selected",),
        unmatched_changes=(Path("charts/app/templates/new.yaml"),),
    )

    out = _capture_stdout(
        lambda: validate_cli._emit_result(
            outcome,
            fmt="json",
            out_dir=outcome.out_dir,
            requested_charts=("app",),
            requested_environments=("dev",),
        )
    )

    diagnostics = json.loads(out)["diagnostics"]
    assert diagnostics["warnings"] == ["nothing selected"]
    assert diagnostics["selection"]["requested_filters"] == {
        "charts": ["app"],
        "environments": ["dev"],
    }


def test_text_table_includes_elapsed_column_when_timings_set(tmp_path: Path) -> None:
    output = _capture_stdout(
        lambda: validate_cli._emit_result(
            _result(), fmt="text", out_dir=tmp_path / "out", timings=True
        )
    )
    assert "Elapsed" in output


def test_resolve_display_none_returns_null() -> None:
    d = validate_cli._resolve_display("none", fmt="text")
    from chart_manager.services.manifest_validation.progress import NullDisplay

    assert isinstance(d, NullDisplay)


def test_resolve_display_plain_returns_plain() -> None:
    from chart_manager.cli.validate_progress import PlainNarrationDisplay

    d = validate_cli._resolve_display("plain", fmt="text")
    assert isinstance(d, PlainNarrationDisplay)


def test_resolve_display_auto_with_json_picks_null() -> None:
    from chart_manager.services.manifest_validation.progress import NullDisplay

    d = validate_cli._resolve_display("auto", fmt="json")
    # JSON output piped through jq must not see progress chatter.
    assert isinstance(d, NullDisplay)


def test_resolve_display_live_without_tty_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chart_manager.cli.validate_progress import PlainNarrationDisplay

    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    d = validate_cli._resolve_display("live", fmt="text")
    assert isinstance(d, PlainNarrationDisplay)


def test_run_help_lists_new_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["validate", "run", "--help"])
    assert result.exit_code == 0
    for flag in ("--workers", "--progress", "--timings", "--verbose"):
        assert flag in result.output


def test_run_rejects_unknown_progress_mode() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["validate", "run", "--progress", "fancy", "--all"])
    assert result.exit_code != 0
    assert "progress" in result.output.lower()


# --- surface -> service handoff --------------------------------------------
#
# The CLI's whole job is now: build a request, hand it to ManifestValidationService,
# render the outcome, apply retention, exit. These drive the commands with a
# fake app so the handoff itself is under test.


class _FakeApp:
    """Records the request it was handed and returns a canned outcome."""

    def __init__(self, outcome: RunOutcome | None = None, error: Exception | None = None):
        self.outcome = outcome
        self.error = error
        self.requests: list[object] = []
        self.cleanups: list[RunOutcome] = []
        self.cleanup_saw_summary: bool | None = None

    def _answer(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.outcome is not None
        return self.outcome

    def run(self, request):
        return self._answer(request)

    def single(self, request):
        return self._answer(request)

    def cleanup(self, outcome: RunOutcome) -> None:
        self.cleanups.append(outcome)
        self.cleanup_saw_summary = (outcome.out_dir / "summary.md").is_file()


def _outcome(out_dir: Path, *, exit_code: int = 0, **kwargs) -> RunOutcome:
    rows = ()
    if exit_code:
        rows = (
            RowResult(
                row=WorklistRow(chart="c", env="dev", release="c", namespace="lab-dev"),
                phases={"render": PhaseResult(phase="render", status="FAIL")},
            ),
        )
    return RunOutcome(result=RunResult(rows=rows, rendered_root=out_dir), out_dir=out_dir, **kwargs)


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeApp) -> None:
    monkeypatch.setattr(validate_cli, "_make_app", lambda progress=None: fake)


def test_run_builds_a_request_from_its_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeApp(_outcome(tmp_path / "out"))
    _install(monkeypatch, fake)

    result = CliRunner().invoke(
        app,
        [
            "validate",
            "run",
            "--all",
            "--chart",
            "alpha",
            "--env",
            "dev",
            "--phases",
            "render,schema",
            "--workers",
            "3",
            "--row-timeout",
            "12",
            "--fail-fast",
            "--root",
            str(tmp_path),
            "--progress",
            "none",
        ],
    )

    assert result.exit_code == 0
    request = fake.requests[0]
    assert request.all_charts is True
    assert request.charts == ("alpha",)
    assert request.envs == ("dev",)
    assert request.phases == frozenset({"render", "schema"})
    assert request.workers == 3
    assert request.row_timeout == 12.0
    assert request.fail_fast is True
    assert request.root == tmp_path


def test_run_defaults_to_continuing_after_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeApp(_outcome(tmp_path / "out"))
    _install(monkeypatch, fake)

    result = CliRunner().invoke(
        app,
        ["validate", "run", "--all", "--progress", "none", "--root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert fake.requests[0].fail_fast is False


def test_run_exits_with_the_outcome_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, _FakeApp(_outcome(tmp_path / "out", exit_code=1)))

    result = CliRunner().invoke(
        app, ["validate", "run", "--all", "--progress", "none", "--root", str(tmp_path)]
    )

    assert result.exit_code == 1


def test_run_applies_retention_only_after_the_summary_is_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--format all writes sidecars into the render dir; cleanup comes last."""
    out_dir = tmp_path / "out"
    fake = _FakeApp(_outcome(out_dir))
    _install(monkeypatch, fake)

    CliRunner().invoke(
        app,
        [
            "validate",
            "run",
            "--all",
            "--progress",
            "none",
            "--format",
            "all",
            "--root",
            str(tmp_path),
        ],
    )

    assert fake.cleanups == [fake.outcome]
    assert fake.cleanup_saw_summary is True


def test_run_maps_a_rejected_input_onto_its_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(
        monkeypatch,
        _FakeApp(error=ValidateInputError("cannot read it", hint="changed_files")),
    )

    result = CliRunner().invoke(
        app, ["validate", "run", "--progress", "none", "--root", str(tmp_path)]
    )

    assert result.exit_code == 2
    assert "--changed-files" in result.output
    assert "cannot read it" in result.output


def test_run_emits_extra_warnings_from_the_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeApp(_outcome(tmp_path / "out", warnings=("chart x has no spec",)))
    _install(monkeypatch, fake)

    output = _capture_stdout(
        lambda: CliRunner().invoke(
            app,
            ["validate", "run", "--all", "--progress", "none", "--root", str(tmp_path)],
        )
    )

    assert "chart x has no spec" in output


def test_run_summary_reports_unvalidated_charts_and_spec_errors(tmp_path: Path) -> None:
    outcome = RunOutcome(
        result=RunResult(
            rows=(),
            rendered_root=tmp_path,
            spec_errors=("charts/broken: boom",),
        ),
        out_dir=tmp_path,
        charts_unvalidated=2,
    )

    output = _capture_stdout(lambda: validate_cli._print_summary(outcome))

    assert "spec error: charts/broken: boom" in output
    assert "1 spec error(s)" in output
    assert "2 chart(s) unvalidated" in output
    assert "0 rows" in output


def _outcome_with_not_run(
    tmp_path: Path, *, enabled: frozenset[str], not_run: frozenset[str]
) -> RunOutcome:
    """One row where `not_run` phases are NOT_RUN, under `enabled` phases.

    `enabled` and `not_run` are independent on purpose: the interesting cases
    are a disabled phase that is NOT_RUN (expected, silent) and an *enabled*
    phase that is NOT_RUN (an anomaly worth a line).
    """
    return RunOutcome(
        result=RunResult(
            rows=(
                RowResult(
                    row=WorklistRow(
                        chart="grafana", env="dev", release="grafana", namespace="lab-dev"
                    ),
                    phases={
                        name: PhaseResult(
                            phase=name,
                            status="NOT_RUN" if name in not_run else "PASS",
                        )
                        for name in ("render", "schema", "policy")
                    },
                ),
            ),
            rendered_root=tmp_path,
        ),
        out_dir=tmp_path,
        enabled_phases=enabled,
    )


def test_summary_ignores_phases_the_caller_disabled(tmp_path: Path) -> None:
    """`--phases render` must not report schema/policy as an anomaly."""
    outcome = _outcome_with_not_run(
        tmp_path,
        enabled=frozenset({"render"}),
        not_run=frozenset({"schema", "policy"}),
    )

    output = _capture_stdout(lambda: validate_cli._print_summary(outcome))

    assert "NOT_RUN" not in output
    assert "summary:" not in output


def test_summary_still_reports_a_not_run_phase_the_caller_asked_for(
    tmp_path: Path,
) -> None:
    """An enabled phase that never ran is a real anomaly and must be counted."""
    outcome = _outcome_with_not_run(
        tmp_path,
        enabled=frozenset({"render", "schema", "policy"}),
        not_run=frozenset({"schema", "policy"}),
    )

    output = _capture_stdout(lambda: validate_cli._print_summary(outcome))

    assert "2 phase(s) NOT_RUN" in output


def test_verbose_forces_plain_progress_and_warns_about_serial_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeApp(_outcome(tmp_path / "out"))
    seen: list[object] = []

    def _make(progress=None):
        seen.append(progress)
        return fake

    monkeypatch.setattr(validate_cli, "_make_app", _make)

    output = _capture_stdout(
        lambda: CliRunner().invoke(
            app,
            ["validate", "run", "--all", "--verbose", "--workers", "4", "--root", str(tmp_path)],
        )
    )

    from chart_manager.cli.validate_progress import PlainNarrationDisplay

    assert isinstance(seen[0], PlainNarrationDisplay)
    assert "--verbose forces --workers=1" in output
    # The service is told the truth; it owns the actual clamp.
    assert fake.requests[0].verbose is True
    assert fake.requests[0].workers == 4


def test_verbose_with_one_worker_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, _FakeApp(_outcome(tmp_path / "out")))

    output = _capture_stdout(
        lambda: CliRunner().invoke(
            app,
            ["validate", "run", "--all", "--verbose", "--workers", "1", "--root", str(tmp_path)],
        )
    )

    assert "forces --workers=1" not in output


@pytest.mark.parametrize(
    ("command", "expected_phases"),
    [
        ("render", frozenset({"render", "schema", "policy"})),
        ("schema", frozenset({"render", "schema", "policy"})),
        ("policy", frozenset({"render", "schema", "policy"})),
    ],
)
def test_single_row_commands_build_single_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    expected_phases: frozenset[str],
) -> None:
    fake = _FakeApp(_outcome(tmp_path / "out"))
    _install(monkeypatch, fake)

    result = CliRunner().invoke(
        app,
        ["validate", command, "--chart", "alpha", "--env", "dev", "--root", str(tmp_path)],
    )

    assert result.exit_code == 0
    request = fake.requests[0]
    assert (request.chart, request.env) == ("alpha", "dev")
    assert request.phases == expected_phases
    assert fake.cleanups == [fake.outcome]


def test_only_the_policy_command_asks_for_policy_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """render/schema must keep passing no policy paths at all (phase SKIPs)."""
    requests = []
    for command in ("render", "schema", "policy"):
        fake = _FakeApp(_outcome(tmp_path / "out"))
        _install(monkeypatch, fake)
        CliRunner().invoke(
            app,
            ["validate", command, "--chart", "a", "--env", "dev", "--root", str(tmp_path)],
        )
        requests.append(fake.requests[0])

    assert [r.discover_policies for r in requests] == [False, False, True]


def test_single_row_command_maps_chart_not_found_onto_the_chart_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, _FakeApp(error=ChartNotFoundError("chart not found: ghost")))

    result = CliRunner().invoke(app, ["validate", "render", "--chart", "ghost", "--env", "dev"])

    assert result.exit_code == 2
    assert "--chart" in result.output
    assert "ghost" in result.output


@pytest.mark.parametrize("command", ["render", "schema", "policy"])
def test_single_row_commands_reject_both_helm_bindings(command: str) -> None:
    result = CliRunner().invoke(
        app,
        [
            "validate",
            command,
            "--chart",
            "a",
            "--env",
            "dev",
            "--helm-version",
            "3.20.0",
            "--helm-bin",
            "/usr/bin/helm",
        ],
    )

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_run_rejects_an_unknown_phase() -> None:
    result = CliRunner().invoke(app, ["validate", "run", "--all", "--phases", "lint"])

    assert result.exit_code == 2
    assert "unknown phase" in result.output


def test_run_rejects_an_empty_phase_list() -> None:
    result = CliRunner().invoke(app, ["validate", "run", "--all", "--phases", " ,"])

    assert result.exit_code == 2
    assert "at least one phase" in result.output
