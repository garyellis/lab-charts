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
from chart_manager.plumbing.validate_models import (
    PhaseResult,
    RowResult,
    RunResult,
    WorklistRow,
)


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


def test_github_step_summary_always_written_even_in_text_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    step_summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    _capture_stdout(
        lambda: validate_cli._emit_result(_result(), fmt="text", out_dir=tmp_path / "out")
    )
    assert step_summary.is_file()
    assert step_summary.read_text().startswith("## validate")


def test_github_step_summary_appends_across_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub aggregates step summaries — verify append mode, not truncate."""
    step_summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    _capture_stdout(
        lambda: validate_cli._emit_result(_result(), fmt="text", out_dir=tmp_path / "out")
    )
    first_len = step_summary.stat().st_size
    _capture_stdout(
        lambda: validate_cli._emit_result(_result(), fmt="text", out_dir=tmp_path / "out")
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
        lambda: validate_cli._emit_result(_result(), fmt="text", out_dir=tmp_path / "out")
    )
    assert "could not write GITHUB_STEP_SUMMARY" in output


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


# --- deps-install CLI -------------------------------------------------


def test_deps_install_rejects_tool_and_all_combined() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app, ["validate", "deps-install", "--all", "--tool", "helm"]
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_deps_install_rejects_unknown_tool() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["validate", "deps-install", "--tool", "terraform"])
    assert result.exit_code != 0
    assert "unknown tool" in result.output


def test_deps_install_defaults_to_install_all(monkeypatch: pytest.MonkeyPatch) -> None:
    from chart_manager.services.validate import deps_install as deps_install_mod

    calls: list[str] = []

    def fake_install_all(runner, *, on_warn=print):
        calls.append("all")
        return [deps_install_mod.InstallResult(tool="helm", version="4.1.3", success=True)]

    def fake_install_one(runner, tool, *, on_warn=print):
        calls.append(f"one:{tool}")
        return []

    monkeypatch.setattr(validate_cli.deps_install_mod, "install_all", fake_install_all)
    monkeypatch.setattr(validate_cli.deps_install_mod, "install_one", fake_install_one)

    runner = CliRunner()
    result = runner.invoke(app, ["validate", "deps-install"])

    assert result.exit_code == 0
    assert calls == ["all"]
    assert "helm@4.1.3: ok" in result.output


def test_deps_install_with_explicit_tools_calls_install_one_per_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chart_manager.services.validate import deps_install as deps_install_mod

    calls: list[str] = []

    def fake_install_one(runner, tool, *, on_warn=print):
        calls.append(tool)
        return [deps_install_mod.InstallResult(tool=tool, version="x", success=True)]

    monkeypatch.setattr(validate_cli.deps_install_mod, "install_one", fake_install_one)

    runner = CliRunner()
    result = runner.invoke(
        app, ["validate", "deps-install", "--tool", "kubeconform", "--tool", "kyverno"]
    )

    assert result.exit_code == 0
    assert calls == ["kubeconform", "kyverno"]


def test_emit_json_includes_elapsed_seconds_when_timings_set(tmp_path: Path) -> None:
    from chart_manager.plumbing.validate_models import (
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


def test_text_table_includes_elapsed_column_when_timings_set(tmp_path: Path) -> None:
    output = _capture_stdout(
        lambda: validate_cli._emit_result(
            _result(), fmt="text", out_dir=tmp_path / "out", timings=True
        )
    )
    assert "Elapsed" in output


def test_resolve_display_none_returns_null() -> None:
    d = validate_cli._resolve_display("none", fmt="text")
    from chart_manager.services.validate.progress import NullDisplay
    assert isinstance(d, NullDisplay)


def test_resolve_display_plain_returns_plain() -> None:
    from chart_manager.services.validate.progress import PlainNarrationDisplay
    d = validate_cli._resolve_display("plain", fmt="text")
    assert isinstance(d, PlainNarrationDisplay)


def test_resolve_display_auto_with_json_picks_null() -> None:
    from chart_manager.services.validate.progress import NullDisplay
    d = validate_cli._resolve_display("auto", fmt="json")
    # JSON output piped through jq must not see progress chatter.
    assert isinstance(d, NullDisplay)


def test_resolve_display_live_without_tty_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chart_manager.services.validate.progress import PlainNarrationDisplay
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


def test_default_workers_floors_at_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 1)
    assert validate_cli._default_workers() == 2


def test_default_workers_ceilings_at_eight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 32)
    assert validate_cli._default_workers() == 8


def test_deps_install_exits_nonzero_on_any_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chart_manager.services.validate import deps_install as deps_install_mod

    def fake_install_all(runner, *, on_warn=print):
        return [
            deps_install_mod.InstallResult(tool="helm", version="4.1.3", success=True),
            deps_install_mod.InstallResult(
                tool="kyverno", version="1.18.1", success=False, detail="boom"
            ),
        ]

    monkeypatch.setattr(validate_cli.deps_install_mod, "install_all", fake_install_all)

    runner = CliRunner()
    result = runner.invoke(app, ["validate", "deps-install", "--all"])

    assert result.exit_code == 1
    assert "kyverno@1.18.1: failed" in result.output
    assert "github.com/kyverno/kyverno/releases/tag/v1.18.1" in result.output
