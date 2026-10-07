"""Typer commands of the validate package: `chart validate`, `chart cache clean`, `schemas sync`."""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast, get_args

import typer
from rich.table import Table

from chart_manager.cli import output as output_mod
from chart_manager.cli._container import container as _container
from chart_manager.cli.streams import console, narration
from chart_manager.commands.validate.display import LiveTable, PlainNarration
from chart_manager.commands.validate.models import (
    CheckName,
    RequestError,
    ValidateOutcome,
    ValidateRequest,
)
from chart_manager.commands.validate.output import details, to_json, to_markdown, to_table
from chart_manager.commands.validate.progress import NULL_PROGRESS, Progress
from chart_manager.commands.validate.render_dir import clean_render_dir, render_dir_state
from chart_manager.commands.validate.run import run
from chart_manager.commands.validate.schemas.app import sync as sync_schemas
from chart_manager.commands.validate.schemas.store import open_schema_store
from chart_manager.integrations.git import Git
from chart_manager.integrations.kubeconform import GitHubKubeconformSchemaSource
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
from chart_manager.shared.charts.chart import resolve_chart_target

_OUTPUTS = (output_mod.TABLE, output_mod.MD, output_mod.JSON, output_mod.ALL)
_CLEAN_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML)
_PROGRESS = ("auto", "live", "plain", "none")


def validate(
    ctx: typer.Context,
    charts: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="[CHART]...",
            help="Charts to validate, by name or directory. Naming none validates what changed.",
        ),
    ] = None,
    chart: Annotated[
        list[str], typer.Option("--chart", help="A chart to validate (repeatable).")
    ] = [],
    env: Annotated[
        list[str], typer.Option("--env", help="Only this environment (repeatable).")
    ] = [],
    base: Annotated[
        str, typer.Option("--base", help="Git base for `git diff --name-only <base>...HEAD`.")
    ] = "origin/main",
    changed_files: Annotated[
        Path | None,
        typer.Option("--changed-files", help="Newline-separated changed paths (skips git)."),
    ] = None,
    all_charts: Annotated[
        bool, typer.Option("--all", help="Every chart in every environment; ignore git.")
    ] = False,
    check: Annotated[
        list[str],
        typer.Option(
            "--check", help="Validation check to run: render, schema or policy (repeatable)."
        ),
    ] = [],
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Render directory. Default: <root>/<spec.renderDir>/<run-id>/."),
    ] = None,
    keep: Annotated[
        bool, typer.Option("--keep/--no-keep", help="Keep rendered output on success.")
    ] = False,
    workers: Annotated[
        int, typer.Option("--workers", help="Rows checked at once. 0 = min(cpu, 8); 1 = serial.")
    ] = 0,
    progress: Annotated[
        str,
        typer.Option("--progress", help="auto (live table on a terminal), live, plain or none."),
    ] = "auto",
    timings: Annotated[
        bool, typer.Option("--timings/--no-timings", help="Show per-row elapsed time.")
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose/--no-verbose",
            help="Stream helm output instead of capturing it. Checks rows one at a time.",
        ),
    ] = False,
    tool_timeout: Annotated[
        float,
        typer.Option(
            "--tool-timeout",
            help="Seconds allowed per helm (dependency updates too), kubeconform or kyverno "
            "call. 0 = no limit.",
        ),
    ] = 0.0,
    fail_fast: Annotated[
        bool,
        typer.Option("--fail-fast/--no-fail-fast", help="Skip the rows after the first failure."),
    ] = False,
    output: Annotated[
        str | None,
        output_mod.output_option(
            *_OUTPUTS, extra_help=" all = table plus summary.md/summary.json in the render dir."
        ),
    ] = None,
    github_step_summary: Annotated[
        bool,
        typer.Option(
            "--github-step-summary", help="Append the markdown summary to $GITHUB_STEP_SUMMARY."
        ),
    ] = False,
) -> None:
    """Render each selected chart in each environment and run its validation checks.

    Rows, first that applies: --all (every chart), --changed-files, a named chart (every
    environment), else `git diff` against --base. A named chart narrows the first two.
    """
    mode = output_mod.resolve(output, ctx, allowed=_OUTPUTS, console=console)
    if progress not in _PROGRESS:
        raise typer.BadParameter(f"allowed: {', '.join(_PROGRESS)}", param_hint="--progress")
    unknown = sorted(set(check) - set(get_args(CheckName)))
    if unknown:
        raise typer.BadParameter(f"unknown check(s): {', '.join(unknown)}", param_hint="--check")
    container = _container()
    workspace = container.workspace()
    selected = (*(charts or ()), *chart)
    if len(selected) == 1:
        try:
            target = resolve_chart_target(workspace, selected[0])
        except SpecError:
            target = None
        if target is not None:
            selected = (target.name,)
            workspace = workspace.with_charts_dir(target.path.parent.relative_to(workspace.root))
    runner = container.command_runner()
    rendered = (out or workspace.render_root / _run_id()).resolve()
    request = ValidateRequest(
        charts=selected,
        envs=tuple(env),
        checks=frozenset(
            cast(CheckName, name) for name in (check if check else get_args(CheckName))
        ),
        changes=_changes(all_charts, changed_files, selected, base, workspace.root, runner),
        out=rendered,
        workers=workers,
        fail_fast=fail_fast,
        tool_timeout=tool_timeout or None,
        verbose=verbose,
    )
    if verbose and workers != 1:
        narration.print(
            "[yellow]warn:[/yellow] --verbose forces --workers=1 to keep "
            "streamed subprocess output readable"
        )
    try:
        outcome = run(
            request,
            workspace=workspace,
            runner=runner,
            schema_cache_root=container.settings.schema_cache_root,
            progress=_display(progress, mode, verbose),
        )
    except RequestError as exc:
        raise typer.BadParameter(str(exc), param_hint=exc.flag) from exc
    try:
        _emit(
            outcome, mode=mode, rendered=rendered, timings=timings, step_summary=github_step_summary
        )
    finally:
        _retain(rendered, keep=keep or out is not None, outcome=outcome)
    raise typer.Exit(code=exit_code_for(outcome.outcome()))


def _changes(
    all_charts: bool,
    changed_files: Path | None,
    selected: tuple[str, ...],
    base: str,
    root: Path,
    runner: CommandRunner,
) -> tuple[str, ...] | None:
    """The changed paths that pick the rows, or None for every row of the selected charts."""
    if all_charts:
        return None
    if changed_files is not None:
        try:
            text = changed_files.read_text()
        except OSError as exc:
            raise typer.BadParameter(f"cannot read: {exc}", param_hint="--changed-files") from exc
        return tuple(line for line in text.splitlines() if line.strip())
    if selected:
        return None
    try:
        return tuple(Git(root, runner).changed_files(base=base))
    except ChartManagerError as exc:
        narration.print(f"[yellow]warn:[/yellow] git diff failed ({exc}); falling back to --all")
        return None


def _display(progress: str, mode: str, verbose: bool) -> Progress:
    """The progress sink for this --progress and output mode; machine output gets none.

    --verbose streams subprocess output, which a live table would overwrite, so it narrates.
    """
    if progress == "none" or mode in (output_mod.JSON, output_mod.MD):
        return NULL_PROGRESS
    if progress == "plain" or verbose:
        return PlainNarration()
    if not sys.stderr.isatty():
        if progress == "live":
            narration.print("[yellow]warn:[/yellow] stderr is not a terminal; using plain progress")
        return PlainNarration()
    return LiveTable() if progress == "live" or mode == output_mod.TABLE else PlainNarration()


def _emit(
    outcome: ValidateOutcome, *, mode: str, rendered: Path, timings: bool, step_summary: bool
) -> None:
    """Print the run in `mode`; with `all`, also write summary.md and summary.json into `rendered`."""
    markdown = to_markdown(outcome, timings=timings)
    if mode == output_mod.JSON:
        sys.stdout.write(json.dumps(to_json(outcome, rendered=rendered), indent=2) + "\n")
    elif mode == output_mod.MD:
        sys.stdout.write(markdown)
    else:
        console.print(to_table(outcome, timings=timings))
        for block in details(outcome):
            console.print(block)
        for warning in outcome.warnings:
            narration.print(f"[yellow]warn:[/yellow] {warning}")
        for error in outcome.spec_errors:
            narration.print(f"[red]spec error:[/red] {error}")
        summary = [f"{len(outcome.spec_errors)} spec error(s)"] if outcome.spec_errors else []
        summary += [] if outcome.rows else ["0 rows"]
        if summary:
            narration.print(f"[bold]summary:[/bold] {'; '.join(summary)}")
    if mode == output_mod.ALL:
        try:
            rendered.mkdir(parents=True, exist_ok=True)
            (rendered / "summary.md").write_text(markdown)
            payload = json.dumps(to_json(outcome, rendered=rendered), indent=2) + "\n"
            (rendered / "summary.json").write_text(payload)
        except OSError as exc:
            narration.print(f"[yellow]warning: could not write summaries ({exc})[/yellow]")
    if step_summary:
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if not path:
            narration.print(
                "[yellow]warning: --github-step-summary was passed but $GITHUB_STEP_SUMMARY "
                "is not set; skipping step summary write[/yellow]"
            )
            return
        try:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(markdown)
        except OSError as exc:
            narration.print(
                f"[yellow]warning: could not write GITHUB_STEP_SUMMARY ({exc})[/yellow]"
            )


def _retain(rendered: Path, *, keep: bool, outcome: ValidateOutcome) -> None:
    """Delete the render dir after a clean run unless kept (or DEBUG=true); never raises."""
    if (
        keep
        or outcome.outcome() is not Outcome.SUCCESS
        or os.environ.get("DEBUG", "").lower() == "true"
    ):
        return
    try:
        shutil.rmtree(rendered)
    except FileNotFoundError:
        return
    except OSError as exc:
        narration.print(f"[yellow]warning: cleanup failed: {exc}[/yellow]")


def clean(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would be removed.")
    ] = False,
    output: Annotated[
        str | None, output_mod.output_option(*_CLEAN_OUTPUTS, extra_help=" Requires --dry-run.")
    ] = None,
) -> None:
    """Remove the workspace's render directory (`spec.renderDir`)."""
    output_mod.require_dry_run(output, dry_run=dry_run)
    workspace = _container().workspace()
    if dry_run:
        state = render_dir_state(workspace)
        mode = output_mod.resolve(output, ctx, allowed=_CLEAN_OUTPUTS, console=console)
        table = Table("Path", "Exists", "Runs", title="render cache")
        table.add_row(str(state.path), "yes" if state.exists else "no", str(state.runs))
        output_mod.emit(state.to_dict(), mode=mode, table=table)
        narration.print("[yellow]dry run[/yellow]: nothing was removed")
        return
    try:
        state = clean_render_dir(workspace)
    except OSError as exc:
        narration.print(f"[red]error:[/red] cleanup failed: {exc}")
        raise typer.Exit(code=exit_code_for(Outcome.FAILED)) from exc
    narration.print(f"cleaned: {state.path}" if state.exists else "nothing to clean")


def sync(
    update: Annotated[
        bool,
        typer.Option(
            "--update",
            help="Resolve tracking refs, cache repositories, and update the lock.",
        ),
    ] = False,
) -> None:
    """Cache complete upstream schema repositories at the committed pins."""
    container = _container()
    timeout = container.settings.command_timeout
    result = sync_schemas(
        container.workspace(),
        store=open_schema_store(
            container.command_runner(), container.settings.schema_cache_root
        ),
        source=GitHubKubeconformSchemaSource(
            timeout=timeout if timeout is not None and timeout > 0 else 15.0,
            github_token=container.settings.github_token,
        ),
        update=update,
    )
    action = "published" if result.generation_published else "ready"
    console.print(
        f"schema generation {result.lock.generation} {action} at {result.generation_path}"
    )


def _run_id() -> str:
    """A render-dir name for one run: UTC time plus a short random suffix."""
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
