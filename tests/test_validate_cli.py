"""CLI-layer tests for the validate sub-app.

We don't shell out to helm/kubeconform/kyverno here — that's integration
territory. These tests exercise the CLI's emission, format routing,
side-file writing, and GITHUB_STEP_SUMMARY behavior by driving the
internal `_emit_result` helper with a fabricated RunResult and by
invoking `--help` / `--output unknown` through Typer's CliRunner.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch

import pytest

from chart_manager.cli import validate as validate_cli
from chart_manager.services.manifest_validation.app import RunOutcome, ValidateInputError
from chart_manager.services.manifest_validation.models import (
    PhaseResult,
    RowResult,
    RunResult,
    WorklistRow,
)

from .conftest import cli


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


class _Captured(NamedTuple):
    """The two streams `cli/validate.py` writes to, kept apart."""

    stdout: str
    stderr: str


def _capture(fn) -> _Captured:
    """Capture the data projection and the narration separately.

    `validate_cli.console` is the `--output` projection and shares a buffer
    with raw `sys.stdout.write` (json/md go out that way); `validate_cli.
    narration` is everything else. Keeping them in two buffers is what lets
    a test assert that a warning did *not* land in the JSON document.
    """
    from rich.console import Console as _Console

    out, err = io.StringIO(), io.StringIO()

    def _recorder(buf: io.StringIO) -> _Console:
        return _Console(file=buf, force_terminal=False, no_color=True, width=200)

    old_console, old_narration = validate_cli.console, validate_cli.narration
    validate_cli.console = _recorder(out)
    validate_cli.narration = _recorder(err)
    try:
        with patch("sys.stdout", out):
            fn()
    finally:
        validate_cli.console = old_console
        validate_cli.narration = old_narration
    return _Captured(out.getvalue(), err.getvalue())


def _capture_stdout(fn) -> str:
    """The data projection only -- see `_capture` for the narration half."""
    return _capture(fn).stdout


def test_emit_json_writes_valid_json_with_schema_version(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    output = _capture_stdout(
        lambda: validate_cli._emit_result(_result(), mode="json", out_dir=out_dir)
    )
    payload = json.loads(output)
    assert payload["schema_version"] == 1
    assert payload["exit_code"] == 0
    assert payload["summary"]["rows"] == 1
    # JSON format must not write summary.md
    assert not (out_dir / "summary.md").exists()


def test_emit_md_writes_markdown_starting_with_heading(tmp_path: Path) -> None:
    output = _capture_stdout(
        lambda: validate_cli._emit_result(_result(), mode="md", out_dir=tmp_path / "out")
    )
    assert output.startswith("## validate")
    assert "| Chart |" in output
    # No Rich text-table glyphs in md mode.
    assert "PASS" not in output  # md uses ✅ not PASS


def test_emit_table_prints_table_and_does_not_emit_summary_md(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    output = _capture_stdout(
        lambda: validate_cli._emit_result(_result(), mode="table", out_dir=out_dir)
    )
    assert "PASS" in output  # text-table cell text
    assert "Chart" in output
    assert not (out_dir / "summary.md").exists()
    assert not (out_dir / "summary.json").exists()


def test_emit_all_prints_table_and_writes_summary_md_and_json(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    output = _capture_stdout(
        lambda: validate_cli._emit_result(_result(), mode="all", out_dir=out_dir)
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
            mode="table",
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
            mode="table",
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
    narration = _capture(
        lambda: validate_cli._emit_result(
            _result(),
            mode="table",
            out_dir=tmp_path / "out",
            github_step_summary=True,
        )
    ).stderr
    assert "GITHUB_STEP_SUMMARY" in narration
    assert "not set" in narration


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
            mode="table",
            out_dir=tmp_path / "out",
            github_step_summary=True,
        )
    )
    first_len = step_summary.stat().st_size
    _capture_stdout(
        lambda: validate_cli._emit_result(
            _result(),
            mode="table",
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
    narration = _capture(
        lambda: validate_cli._emit_result(
            _result(),
            mode="table",
            out_dir=tmp_path / "out",
            github_step_summary=True,
        )
    ).stderr
    assert "could not write GITHUB_STEP_SUMMARY" in narration


def test_validate_help_lists_github_step_summary_flag() -> None:
    """One command, one help page.

    Was parametrized over `validate chart` and `validate run`, which were two
    names for one function. Both are now spelled `chart validate`, so the
    parameter distinguished nothing and the second case asserted twice about
    the same help text.
    """
    result = cli("chart", "validate", "--help")
    assert result.exit_code == 0
    assert "--github-step-summary" in result.output


def test_output_option_rejects_unknown_value() -> None:
    # Validation moved to the `-o/--output` parse-time callback in
    # cli/output.py -- `_validate_format` no longer exists.
    result = cli("chart", "validate", "--output", "yaml")
    assert result.exit_code == 2
    assert "yaml" in result.output
    assert "table" in result.output  # lists allowed values


def test_validate_help_lists_output_option() -> None:
    """See `test_validate_help_lists_github_step_summary_flag`.

    This absorbed `test_each_subcommand_help_lists_output_option`, which was
    the same assertion parametrized over the two old spellings.
    """
    result = cli("chart", "validate", "--help")
    assert result.exit_code == 0
    assert "--output" in result.output


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
            result, mode="json", out_dir=tmp_path / "out", timings=True
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
        lambda: validate_cli._emit_result(_result(), mode="json", out_dir=tmp_path / "out")
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
            mode="json",
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
            _result(), mode="table", out_dir=tmp_path / "out", timings=True
        )
    )
    assert "Elapsed" in output


def test_resolve_display_none_returns_null() -> None:
    d = validate_cli._resolve_display("none", mode="table")
    from chart_manager.services.manifest_validation.progress import NullDisplay

    assert isinstance(d, NullDisplay)


def test_resolve_display_plain_returns_plain() -> None:
    from chart_manager.cli.validate_progress import PlainNarrationDisplay

    d = validate_cli._resolve_display("plain", mode="table")
    assert isinstance(d, PlainNarrationDisplay)


def test_resolve_display_auto_with_json_picks_null() -> None:
    from chart_manager.services.manifest_validation.progress import NullDisplay

    d = validate_cli._resolve_display("auto", mode="json")
    # JSON output piped through jq must not see progress chatter.
    assert isinstance(d, NullDisplay)


def test_resolve_display_live_without_tty_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chart_manager.cli.validate_progress import PlainNarrationDisplay

    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    d = validate_cli._resolve_display("live", mode="table")
    assert isinstance(d, PlainNarrationDisplay)


def test_run_help_lists_new_flags() -> None:
    result = cli("chart", "validate", "--help")
    assert result.exit_code == 0
    for flag in ("--workers", "--progress", "--timings", "--verbose"):
        assert flag in result.output


def test_run_rejects_unknown_progress_mode() -> None:
    result = cli("chart", "validate", "--progress", "fancy", "--all")
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


def test_chart_resolves_a_bare_configured_name_through_the_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bare name under the configured charts dir is resolved, not guessed.

    The surface passes the raw token to `resolve_chart_target` in every case;
    there is no path heuristic in `cli/` deciding whether to call it.
    """
    chart_path = tmp_path / "charts" / "alpha"
    chart_path.mkdir(parents=True)
    (chart_path / "Chart.yaml").write_text(
        "apiVersion: v2\nname: alpha\nversion: 1.0.0\n", encoding="utf-8"
    )
    calls: list[tuple[object, dict[str, object]]] = []

    def execute(request, **options):  # type: ignore[no-untyped-def]
        calls.append((request, options))

    monkeypatch.setattr(validate_cli, "_execute", execute)

    result = cli("chart", "validate", "--chart", "alpha", "--all", "--root", str(tmp_path))

    assert result.exit_code == 0
    request, options = calls[0]
    assert request.charts == ("alpha",)
    assert options["charts_dir"] == Path("charts")


def test_chart_leaves_an_unresolvable_name_to_the_validation_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unknown bare name must not surface a LocalStack-flavored error.

    `resolve_chart_target` falls through to LocalStack-name resolution for a
    single-part token it cannot place on disk. The surface swallows that and
    forwards what the user typed, so the validation service raises the
    precise "unknown chart" error instead.
    """
    calls: list[tuple[object, dict[str, object]]] = []

    def execute(request, **options):  # type: ignore[no-untyped-def]
        calls.append((request, options))

    monkeypatch.setattr(validate_cli, "_execute", execute)

    result = cli("chart", "validate", "--chart", "ghost", "--all", "--root", str(tmp_path))

    assert result.exit_code == 0
    request, options = calls[0]
    assert request.charts == ("ghost",)
    assert options["charts_dir"] is None


def test_chart_delegates_to_shared_execution_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    def execute(request, **options):  # type: ignore[no-untyped-def]
        calls.append((request, options))

    monkeypatch.setattr(validate_cli, "_execute", execute)

    result = cli(
        "chart",
        "validate",
        "--chart",
        "alpha",
        "--env",
        "dev",
        "--no-policy",
        "--keep",
        "--out",
        str(tmp_path / "rendered"),
        "--progress",
        "plain",
        "--timings",
        "--output",
        "md",
        "--github-step-summary",
        "--root",
        str(tmp_path),
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    request, options = calls[0]
    assert request.charts == ("alpha",)
    assert request.envs == ("dev",)
    assert request.skip_change_detection is True
    assert request.phases == frozenset({"render", "schema"})
    assert request.keep is True
    assert request.out == tmp_path / "rendered"
    assert request.root == tmp_path
    assert options == {
        "progress": "plain",
        "timings": True,
        "mode": "md",
        "github_step_summary": True,
        "charts_dir": None,
    }


@pytest.mark.parametrize("absolute", [False, True])
def test_chart_accepts_a_chart_directory_like_charts_test(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    absolute: bool,
) -> None:
    chart_path = tmp_path / "fixtures" / "cert-manager"
    chart_path.mkdir(parents=True)
    (chart_path / "Chart.yaml").write_text(
        "apiVersion: v2\nname: cert-manager\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    calls: list[tuple[object, dict[str, object]]] = []

    def execute(request, **options):  # type: ignore[no-untyped-def]
        calls.append((request, options))

    monkeypatch.setattr(validate_cli, "_execute", execute)
    chart_arg = str(chart_path if absolute else Path("fixtures/cert-manager"))

    result = cli(
        "chart",
        "validate",
        "--chart",
        chart_arg,
        "--all",
        "--root",
        str(tmp_path),
    )

    assert result.exit_code == 0
    request, options = calls[0]
    assert request.root == tmp_path
    assert request.charts == ("cert-manager",)
    assert options["charts_dir"] == Path("fixtures")


def test_run_delegates_to_shared_execution_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    changed = tmp_path / "changed.txt"

    def execute(request, **options):  # type: ignore[no-untyped-def]
        calls.append((request, options))

    monkeypatch.setattr(validate_cli, "_execute", execute)

    result = cli(
        "chart",
        "validate",
        "--chart",
        "alpha",
        "--env",
        "dev",
        "--base",
        "main",
        "--changed-files",
        str(changed),
        "--all",
        "--phase",
        "render",
        "--phase",
        "policy",
        "--out",
        str(tmp_path / "rendered"),
        "--keep",
        "--workers",
        "3",
        "--progress",
        "none",
        "--timings",
        "--verbose",
        "--row-timeout",
        "12",
        "--dep-update-timeout",
        "42",
        "--fail-fast",
        "--output",
        "all",
        "--github-step-summary",
        "--root",
        str(tmp_path),
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    request, options = calls[0]
    assert request.charts == ("alpha",)
    assert request.envs == ("dev",)
    assert request.base == "main"
    assert request.changed_files == changed
    assert request.skip_change_detection is True
    assert request.phases == frozenset({"render", "policy"})
    assert request.out == tmp_path / "rendered"
    assert request.keep is True
    assert request.workers == 3
    assert request.verbose is True
    assert request.row_timeout == 12.0
    assert request.dep_update_timeout == 42.0
    assert request.fail_fast is True
    assert request.root == tmp_path
    assert options == {
        "progress": "none",
        "timings": True,
        "mode": "all",
        "github_step_summary": True,
        # The merged command always reports what it resolved, even when that
        # is "nothing": `alpha` is not on disk under this root, so the
        # repository-wide charts dir stands. `_execute` treats None and
        # omitted identically.
        "charts_dir": None,
    }


def test_run_builds_a_request_from_its_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeApp(_outcome(tmp_path / "out"))
    _install(monkeypatch, fake)

    result = cli(
        "chart",
        "validate",
        "--all",
        "--chart",
        "alpha",
        "--env",
        "dev",
        "--phase",
        "render",
        "--phase",
        "schema",
        "--workers",
        "3",
        "--row-timeout",
        "12",
        "--fail-fast",
        "--root",
        str(tmp_path),
        "--progress",
        "none",
    )

    assert result.exit_code == 0
    request = fake.requests[0]
    assert request.skip_change_detection is True
    assert request.charts == ("alpha",)
    assert request.envs == ("dev",)
    assert request.phases == frozenset({"render", "schema"})
    assert request.workers == 3
    assert request.row_timeout == 12.0
    assert request.fail_fast is True
    assert request.root == tmp_path


# --- the `validate chart` + `validate run` merge ---------------------------
#
# One command now serves two, so the three clauses that make that lossless
# each get a test. Without them the merge is asserted only by
# `tests/test_cli_aliases.py`, which proves the aliases reach this function
# but says nothing about whether this function still means what they meant.


def test_a_chart_is_selected_the_same_way_however_it_is_spelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`chart validate X` and `chart validate --chart X` are one request.

    `--chart` is `validate run`'s spelling and is kept so its callers -- the
    `validate chart` alias among them -- keep working. If the two spellings
    built different requests, the alias would be a rename in name only.
    """
    fake = _FakeApp(_outcome(tmp_path / "out"))
    _install(monkeypatch, fake)
    tail = ("--progress", "none", "--root", str(tmp_path))

    positional = cli("chart", "validate", "alpha", *tail)
    flag = cli("chart", "validate", "--chart", "alpha", *tail)

    assert positional.exit_code == flag.exit_code == 0
    assert fake.requests[0] == fake.requests[1]
    assert fake.requests[0].charts == ("alpha",)
    assert fake.requests[0].skip_change_detection is True


def test_an_explicit_changed_file_list_still_narrows_a_named_chart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--changed-files` outranks a named chart, as it did for `validate run`.

    This is the one combination where "named chart means validate it" would
    have changed `validate run --chart X --changed-files F` from "the
    environments F touches, within X" to "every environment of X" -- silently
    widening a CI run that exists to be narrow. Change detection stays on, so
    the service reads the file.
    """
    changed = tmp_path / "changed.txt"
    changed.write_text("charts/alpha/values.yaml\n", encoding="utf-8")
    fake = _FakeApp(_outcome(tmp_path / "out"))
    _install(monkeypatch, fake)

    result = cli(
        "chart", "validate",
        "--chart", "alpha",
        "--changed-files", str(changed),
        "--progress", "none",
        "--root", str(tmp_path),
    )

    assert result.exit_code == 0
    assert fake.requests[0].skip_change_detection is False
    assert fake.requests[0].changed_files == changed


def test_no_policy_subtracts_from_the_selected_phases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--no-kubeconform`/`--no-policy` narrow `--phase` rather than replacing it.

    `validate chart` built its phase set from these two flags alone. Making
    them subtractive means that at the `--phase` default they reproduce that
    set exactly, and that combining them with an explicit `--phase` -- newly
    possible, since the two commands are one -- narrows instead of surprising.
    """
    fake = _FakeApp(_outcome(tmp_path / "out"))
    _install(monkeypatch, fake)

    result = cli(
        "chart", "validate",
        "--all",
        "--phase", "render",
        "--phase", "policy",
        "--no-policy",
        "--progress", "none",
        "--root", str(tmp_path),
    )

    assert result.exit_code == 0
    assert fake.requests[0].phases == frozenset({"render"})


def test_run_defaults_to_continuing_after_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeApp(_outcome(tmp_path / "out"))
    _install(monkeypatch, fake)

    result = cli("chart", "validate", "--all", "--progress", "none", "--root", str(tmp_path))

    assert result.exit_code == 0
    assert fake.requests[0].fail_fast is False


def test_run_exits_with_the_outcome_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, _FakeApp(_outcome(tmp_path / "out", exit_code=1)))

    result = cli("chart", "validate", "--all", "--progress", "none", "--root", str(tmp_path))

    assert result.exit_code == 1


def test_run_applies_retention_only_after_the_summary_is_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--output all writes sidecars into the render dir; cleanup comes last."""
    out_dir = tmp_path / "out"
    fake = _FakeApp(_outcome(out_dir))
    _install(monkeypatch, fake)

    cli(
        "chart",
        "validate",
        "--all",
        "--progress",
        "none",
        "--output",
        "all",
        "--root",
        str(tmp_path),
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

    result = cli("chart", "validate", "--progress", "none", "--root", str(tmp_path))

    assert result.exit_code == 2
    assert "--changed-files" in result.output
    assert "cannot read it" in result.output


def test_run_emits_extra_warnings_from_the_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeApp(_outcome(tmp_path / "out", warnings=("chart x has no spec",)))
    _install(monkeypatch, fake)

    # Explicit --output table: extra_warnings are only narrated on the
    # table/all path (see _emit_result), and the default `-o auto` would
    # resolve to json in this non-TTY test environment, silently dropping
    # the assertion below without an unrelated-looking failure.
    narration = _capture(
        lambda: cli(
            "chart",
            "validate",
            "--all",
            "--progress",
            "none",
            "--output",
            "table",
            "--root",
            str(tmp_path),
        )
    ).stderr

    assert "chart x has no spec" in narration


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

    narration = _capture(lambda: validate_cli._print_summary(outcome)).stderr

    assert "spec error: charts/broken: boom" in narration
    assert "1 spec error(s)" in narration
    assert "2 chart(s) unvalidated" in narration
    assert "0 rows" in narration


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
    """`--phase render` must not report schema/policy as an anomaly."""
    outcome = _outcome_with_not_run(
        tmp_path,
        enabled=frozenset({"render"}),
        not_run=frozenset({"schema", "policy"}),
    )

    narration = _capture(lambda: validate_cli._print_summary(outcome)).stderr

    assert "NOT_RUN" not in narration
    assert "summary:" not in narration


def test_summary_still_reports_a_not_run_phase_the_caller_asked_for(
    tmp_path: Path,
) -> None:
    """An enabled phase that never ran is a real anomaly and must be counted."""
    outcome = _outcome_with_not_run(
        tmp_path,
        enabled=frozenset({"render", "schema", "policy"}),
        not_run=frozenset({"schema", "policy"}),
    )

    narration = _capture(lambda: validate_cli._print_summary(outcome)).stderr

    assert "2 phase(s) NOT_RUN" in narration


def test_verbose_forces_plain_progress_and_warns_about_serial_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeApp(_outcome(tmp_path / "out"))
    seen: list[object] = []

    def _make(progress=None):
        seen.append(progress)
        return fake

    monkeypatch.setattr(validate_cli, "_make_app", _make)

    # Explicit --output table: `_resolve_display` picks NullDisplay whenever
    # mode is json/md (see its docstring), which would mask the plain-display
    # assertion below under the default `-o auto` in this non-TTY test
    # environment.
    narration = _capture(
        lambda: cli(
            "chart",
            "validate",
            "--all",
            "--verbose",
            "--workers",
            "4",
            "--output",
            "table",
            "--root",
            str(tmp_path),
        )
    ).stderr

    from chart_manager.cli.validate_progress import PlainNarrationDisplay

    assert isinstance(seen[0], PlainNarrationDisplay)
    assert "--verbose forces --workers=1" in narration
    # The service is told the truth; it owns the actual clamp.
    assert fake.requests[0].verbose is True
    assert fake.requests[0].workers == 4


def test_verbose_with_one_worker_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, _FakeApp(_outcome(tmp_path / "out")))

    narration = _capture(
        lambda: cli(
            "chart", "validate", "--all", "--verbose", "--workers", "1", "--root", str(tmp_path)
        )
    ).stderr

    assert "forces --workers=1" not in narration


def test_chart_builds_a_spec_driven_all_environments_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeApp(_outcome(tmp_path / "out"))
    _install(monkeypatch, fake)

    result = cli(
        "chart",
        "validate",
        "--chart",
        "alpha",
        "--all",
        "--progress",
        "none",
        "--root",
        str(tmp_path),
    )

    assert result.exit_code == 0
    request = fake.requests[0]
    assert request.charts == ("alpha",)
    assert request.envs == ()
    assert request.skip_change_detection is True
    assert request.phases == frozenset({"render", "schema", "policy"})
    assert fake.cleanups == [fake.outcome]


def test_chart_honors_environment_and_validator_toggles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeApp(_outcome(tmp_path / "out"))
    _install(monkeypatch, fake)
    result = cli(
        "chart",
        "validate",
        "--chart",
        "alpha",
        "--env",
        "dev",
        "--no-kubeconform",
        "--policy",
        "--progress",
        "none",
        "--root",
        str(tmp_path),
    )

    assert result.exit_code == 0
    request = fake.requests[0]
    assert request.envs == ("dev",)
    assert request.phases == frozenset({"render", "policy"})


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--chart", "alpha"], id="no-environment-selection"),
        pytest.param(["--chart", "alpha", "--env", "dev", "--all"], id="--all-with---env"),
    ],
)
def test_naming_a_chart_no_longer_requires_an_environment_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str]
) -> None:
    """The merged command drops `validate chart`'s `--env` xor `--all` rule.

    That rule existed because `validate chart` had no other way to say "this
    chart, every environment" -- it never consulted git, so an empty `envs`
    would have been ambiguous. The merged command resolves that ambiguity
    from argv instead: a named chart already means "validate it", so no
    `--env` means every declared environment, exactly as `--all` did. Keeping
    the rule would have made `chart validate grafana` -- the design doc's own
    example -- an error.

    `--all` alongside `--env` is likewise now legal, because that is what
    `validate run` always did (see
    `test_run_builds_a_request_from_its_flags`), and one merged command
    cannot enforce both halves of a contradiction.
    """
    fake = _FakeApp(_outcome(tmp_path / "out"))
    _install(monkeypatch, fake)

    result = cli("chart", "validate", *args, "--progress", "none", "--root", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert fake.requests[0].charts == ("alpha",)
    assert fake.requests[0].skip_change_detection is True


@pytest.mark.parametrize("command", ["render", "schema", "policy"])
def test_phase_named_commands_are_not_a_surface(command: str) -> None:
    """A validation *phase* is a `--phases` value, never a command.

    Retargeted from `validate <phase>` to `chart <phase>`: the whole
    `validate` group is gone, so asserting against it would have passed for
    the wrong reason -- "no such group" rather than "no such command". The
    property under test is that the phases did not reappear as commands in
    the group that now owns validation.
    """
    result = cli("chart", command, "--help")

    assert result.exit_code == 2
    assert "No such command" in result.output


def test_run_rejects_an_unknown_phase() -> None:
    result = cli("chart", "validate", "--all", "--phase", "lint")

    assert result.exit_code == 2
    assert "unknown phase" in result.output


def test_run_rejects_an_empty_phase_list() -> None:
    # `--phase ""` -- given, but blank -- is the new spelling of "empty";
    # an omitted `--phase` means "all three phases" instead.
    result = cli("chart", "validate", "--all", "--phase", "")

    assert result.exit_code == 2
    assert "--phase needs a phase name" in result.output


# --- `chart cache clean --dry-run` -------------------------------------------


def _render_cache(root: Path, runs: int = 2) -> Path:
    """A render cache holding `runs` validate-run directories."""
    cache = root / ".chart-manager" / "rendered"
    for index in range(runs):
        (cache / f"run-{index}").mkdir(parents=True)
    return cache


def test_cache_clean_dry_run_removes_nothing(tmp_path: Path) -> None:
    """6.3: print the plan, exit 0, mutate nothing. The tree must survive."""
    cache = _render_cache(tmp_path)

    result = cli("chart", "cache", "clean", "--dry-run", "--root", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert cache.is_dir()
    payload = json.loads(result.stdout)
    assert payload == {"path": str(cache.resolve()), "exists": True, "runs": 2}
    assert "dry run" in result.stderr


def test_cache_clean_dry_run_describes_a_cache_that_is_not_there(
    tmp_path: Path,
) -> None:
    """"Nothing to remove" is an answer, not an error: still exit 0, still a plan."""
    result = cli("chart", "cache", "clean", "--dry-run", "--root", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["exists"] is False


def test_cache_clean_dry_run_renders_a_table(tmp_path: Path) -> None:
    _render_cache(tmp_path, runs=3)

    result = cli(
        "chart", "cache", "clean", "--dry-run", "-o", "table", "--root", str(tmp_path)
    )

    assert result.exit_code == 0, result.output
    assert "render cache" in result.stdout
    assert "yes" in result.stdout


def test_cache_clean_output_without_dry_run_is_a_usage_error(tmp_path: Path) -> None:
    """A real clean emits no document, so `-o` must not be quietly ignored.

    The tree is still there afterwards, which is the half that matters: the
    rejection has to happen *before* the rmtree, not after it.
    """
    cache = _render_cache(tmp_path)

    result = cli("chart", "cache", "clean", "-o", "json", "--root", str(tmp_path))

    assert result.exit_code == 2
    assert "--dry-run" in result.output
    assert cache.is_dir()


def test_cache_clean_still_removes_the_tree_without_dry_run(tmp_path: Path) -> None:
    """Guard the guard: the dry-run tests only mean something if this works."""
    cache = _render_cache(tmp_path)

    result = cli("chart", "cache", "clean", "--root", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert not cache.exists()
    assert "cleaned:" in result.stderr
