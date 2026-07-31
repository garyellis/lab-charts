"""`chart-manager validate` sub-app.

Thin CLI shell over `services/manifest_validation/app.ManifestValidationService`: flag shape and
help text, progress-display choice, output-format dispatch, and the
mapping from domain errors to Typer's `BadParameter`. Everything that
changes the *answer* — worklist construction, row assembly, helm binding,
workers, run identity, artifact retention — lives in the service.

Commands register themselves onto a Typer app passed in by cli/main.py.
This `register(app)` pattern keeps cli/main.py free of validate-specific
imports and lets the sub-app grow without touching main.py.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer

from chart_manager.cli.streams import data_console, narration_console
from chart_manager.cli.validate_progress import (
    LiveTableDisplay,
    PlainNarrationDisplay,
)
from chart_manager.cli.validate_render import (
    advisory_details,
    failure_details,
    to_text_table,
)
from chart_manager.composition import Container, Settings
from chart_manager.plumbing.errors import SpecError
from chart_manager.services.local_resources import resolve_chart_target
from chart_manager.services.manifest_validation.app import (
    ALL_PHASES,
    ManifestValidationService,
    RunOutcome,
    RunRequest,
    ValidateInputError,
    resolve_workers,
)
from chart_manager.services.manifest_validation.models import PHASE_ORDER, RunResult
from chart_manager.services.manifest_validation.progress import NullDisplay, ProgressDisplay
from chart_manager.services.manifest_validation.wire import to_json, to_markdown

_FORMATS = ("text", "md", "json", "all")
_PROGRESS_MODES = ("auto", "live", "plain", "none")
# Maps a domain error's `hint` (an input name) onto the flag that carries it.
_PARAM_HINTS = {
    "changed_files": "--changed-files",
    "phases": "--phases",
}


def _validate_format(value: str) -> str:
    """Return `value` if it is a known --format, else raise BadParameter."""
    if value not in _FORMATS:
        raise typer.BadParameter(
            f"unknown format: {value} (allowed: {', '.join(_FORMATS)})",
            param_hint="--format",
        )
    return value


# --- Shared option declarations -------------------------------------------
#
# Shared options for the spec-driven chart and repository commands.
ChartOption = Annotated[
    str,
    typer.Option(
        "--chart",
        help="Chart name or chart directory.",
    ),
]
OutOption = Annotated[
    Path | None,
    typer.Option(
        "--out", help="Render output dir. Defaults to <root>/.chart-manager/rendered/<run-id>/."
    ),
]
KeepOption = Annotated[
    bool,
    typer.Option("--keep/--no-keep", help="Keep rendered output on success."),
]
FormatOption = Annotated[
    str,
    typer.Option(
        "--format",
        help="Output format: text (default), md, json, or all.",
        # Validated at parse time, not at emission time. `_validate_format`
        # is only reachable from inside `_emit_result`, which runs *after*
        # the whole helm/kubeconform/kyverno pipeline -- so a typo cost a
        # full validate run, and the BadParameter it raised unwound past
        # `app.cleanup(outcome)`, orphaning the rendered tree on disk.
        callback=_validate_format,
    ),
]
GithubStepSummaryOption = Annotated[
    bool,
    typer.Option(
        "--github-step-summary",
        help=(
            "Append the markdown summary to $GITHUB_STEP_SUMMARY. "
            "Opt-in: without this flag the env var is ignored. "
            "If the flag is set but the env var is unset, a warning is "
            "printed and no file is written (so local invocations with "
            "the flag don't fail)."
        ),
    ),
]
RootOption = Annotated[Path, typer.Option("--root", help="Repository root.")]

#: The selected `--format` projection. Goes to stdout.
console = data_console()
#: Warnings, spec errors, summaries. Goes to stderr -- these used to share
#: the stdout console with the `--format json` payload written at
#: `_emit_result`, which corrupted the JSON document in band.
narration = narration_console()


def register(app: typer.Typer) -> None:
    """Attach the validate subcommands to the given Typer app."""
    app.command("chart")(chart)
    app.command("run")(run)
    app.command("clean")(clean)


def _container() -> Container:
    """Build the composition root for one CLI invocation."""
    return Container()


def _make_app(
    progress: ProgressDisplay | None = None,
    *,
    charts_dir: Path | None = None,
) -> ManifestValidationService:
    """Build the ManifestValidationService (module-level so tests can override)."""
    return _container().validate_app(
        progress=progress,
        on_warn=_warn,
        charts_dir=charts_dir,
    )


def _warn(message: str) -> None:
    """Print a service-emitted operator warning."""
    narration.print(f"[yellow]{message}[/yellow]")


def chart(
    chart: ChartOption,
    env: Annotated[
        list[str],
        typer.Option(
            "--env",
            help="Environment declared in chart-lifecycle.yaml (repeatable).",
        ),
    ] = [],
    all_envs: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Validate every environment declared for this chart.",
        ),
    ] = False,
    kubeconform: Annotated[
        bool,
        typer.Option(
            "--kubeconform/--no-kubeconform",
            help="Enable or disable kubeconform schema validation for this invocation.",
        ),
    ] = True,
    policy: Annotated[
        bool,
        typer.Option(
            "--policy/--no-policy",
            help="Enable or disable policy validation for this invocation.",
        ),
    ] = True,
    out: OutOption = None,
    keep: KeepOption = False,
    progress: Annotated[
        str,
        typer.Option(
            "--progress",
            help="Progress UI: auto (default), live, plain, or none.",
        ),
    ] = "auto",
    timings: Annotated[
        bool,
        typer.Option("--timings/--no-timings", help="Include per-phase elapsed times."),
    ] = False,
    fmt: FormatOption = "text",
    github_step_summary: GithubStepSummaryOption = False,
    root: RootOption = Path("."),
) -> None:
    """Validate one chart using its authored lifecycle environments and inputs."""
    if all_envs and env:
        raise typer.BadParameter(
            "--all and --env are mutually exclusive",
            param_hint="--all / --env",
        )
    if not all_envs and not env:
        raise typer.BadParameter(
            "select at least one --env or use --all",
            param_hint="--env / --all",
        )
    phases = {"render"}
    if kubeconform:
        phases.add("schema")
    if policy:
        phases.add("policy")
    root = root.resolve()
    settings = Settings()
    charts_dir: Path | None = None
    try:
        target = resolve_chart_target(
            root,
            chart,
            charts_dir=settings.charts_dir,
            local_config=settings.local_config,
        )
    except SpecError:
        # The resolver owns name/path resolution; the surface must not
        # second-guess it with a path heuristic. When it cannot place the
        # token on disk we pass what the user typed straight through, so the
        # validation service — which owns the chart namespace — raises the
        # precise "unknown chart" error listing the available names.
        pass
    else:
        chart = target.name
        charts_dir = target.path.parent.relative_to(root)
    request = RunRequest(
        root=root,
        charts=(chart,),
        envs=() if all_envs else tuple(env),
        skip_change_detection=True,
        phases=frozenset(phases),
        out=out,
        keep=keep,
    )
    _execute(
        request,
        progress=progress,
        timings=timings,
        fmt=fmt,
        github_step_summary=github_step_summary,
        charts_dir=charts_dir,
    )


def run(
    chart: Annotated[
        list[str],
        typer.Option("--chart", help="Restrict worklist to this chart (repeatable)."),
    ] = [],
    env: Annotated[
        list[str],
        typer.Option("--env", help="Restrict worklist to this environment (repeatable)."),
    ] = [],
    base: Annotated[
        str,
        typer.Option(
            "--base",
            help="Git base ref for `git diff --name-only <base>...HEAD`. Default origin/main.",
        ),
    ] = "origin/main",
    changed_files: Annotated[
        Path | None,
        typer.Option(
            "--changed-files",
            help="Read newline-delimited changed paths from this file (skips git).",
        ),
    ] = None,
    all_charts: Annotated[
        bool,
        typer.Option("--all", help="Validate every chart x env in every spec; ignore git."),
    ] = False,
    phases: Annotated[
        str,
        typer.Option(
            "--phases",
            help="Comma-separated subset of render,schema,policy. Default: all three.",
        ),
    ] = "render,schema,policy",
    out: OutOption = None,
    keep: KeepOption = False,
    workers: Annotated[
        int,
        typer.Option(
            "--workers",
            help=(
                "Concurrent worker threads. 0 = auto (max(2, min(cpu_count, 8))). "
                "1 = serial. Higher values parallelize across rows; each "
                "worker may run `helm template` and friends. The auto-cap of "
                "8 keeps memory bounded on beefy CI runners; raise explicitly "
                "if your runner has >8 cores AND your workload tolerates it."
            ),
        ),
    ] = 0,
    progress: Annotated[
        str,
        typer.Option(
            "--progress",
            help=(
                "Progress UI: auto (default; live in TTY+text, plain otherwise), live, plain, none."
            ),
        ),
    ] = "auto",
    timings: Annotated[
        bool,
        typer.Option(
            "--timings/--no-timings",
            help=(
                "Include per-phase elapsed times in the text/markdown output. "
                "JSON output ALWAYS includes the elapsed_seconds field (null "
                "when not measured) regardless of this flag. Under --workers>1 "
                "wall-clock time INCLUDES wait-for-CPU under contention, not "
                "pure phase work — don't read JSON timings as pure execution."
            ),
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose/--no-verbose",
            help=(
                "Stream subprocess stdout/stderr (helm/kubeconform/kyverno) "
                "instead of capturing. Forces --progress plain. Useful for "
                "debugging hangs."
            ),
        ),
    ] = False,
    row_timeout: Annotated[
        float,
        typer.Option(
            "--row-timeout",
            help=(
                "Per-subprocess wall-clock cap in seconds for each phase "
                "invocation (helm template / kubeconform / kyverno). Applies "
                "per phase, NOT per row total; a 3-phase row can take up to "
                "3x this value. 0 = unbounded (default)."
            ),
        ),
    ] = 0.0,
    dep_update_timeout: Annotated[
        float,
        typer.Option(
            "--dep-update-timeout",
            help=(
                "Wall-clock cap in seconds for each `helm dependency update` "
                "call in the pre-fetch pass. Guards against hung OCI/DNS "
                "lookups. Default 300s. 0 = unbounded."
            ),
        ),
    ] = 300.0,
    fail_fast: Annotated[
        bool,
        typer.Option(
            "--fail-fast/--no-fail-fast",
            help=(
                "Stop after the first failed row and mark remaining rows NOT_RUN. "
                "Default: continue and report every row."
            ),
        ),
    ] = False,
    fmt: FormatOption = "text",
    github_step_summary: GithubStepSummaryOption = False,
    root: RootOption = Path("."),
) -> None:
    """Build a lifecycle worklist, then render and run selected validators.

    Worklist selection precedence:
      --all > --changed-files > explicit --chart > git diff against --base.
    """
    enabled_phases = _parse_phases(phases)
    request = RunRequest(
        root=root,
        charts=tuple(chart),
        envs=tuple(env),
        base=base,
        changed_files=changed_files,
        skip_change_detection=all_charts,
        phases=enabled_phases,
        out=out,
        keep=keep,
        workers=workers,
        verbose=verbose,
        row_timeout=row_timeout,
        dep_update_timeout=dep_update_timeout,
        fail_fast=fail_fast,
    )
    _execute(
        request,
        progress=progress,
        timings=timings,
        fmt=fmt,
        github_step_summary=github_step_summary,
    )


def _execute(
    request: RunRequest,
    *,
    progress: str,
    timings: bool,
    fmt: str,
    github_step_summary: bool,
    charts_dir: Path | None = None,
) -> None:
    """Execute one prepared request and own all CLI-side run behavior.

    ``chart`` and ``run`` differ only in how they select work and build a
    :class:`RunRequest`. This boundary keeps service invocation, presentation,
    retention, and process exit identical without coupling one Typer command
    to another command's Python defaults.
    """
    if progress not in _PROGRESS_MODES:
        raise typer.BadParameter(
            f"unknown progress mode: {progress} (allowed: {', '.join(_PROGRESS_MODES)})",
            param_hint="--progress",
        )

    # --verbose streams raw subprocess stdout/stderr. Live can't share the
    # terminal with that, and >1 worker interleaves the streams into
    # illegible noise — the service forces serial; we say so.
    if request.verbose and progress in ("auto", "live"):
        progress = "plain"
    if request.verbose and resolve_workers(request.workers) > 1:
        narration.print(
            "[yellow]warn:[/yellow] --verbose forces --workers=1 to keep "
            "streamed subprocess output readable"
        )

    display = _resolve_display(progress, fmt=fmt)

    app = (
        _make_app(display)
        if charts_dir is None
        else _make_app(display, charts_dir=charts_dir)
    )
    try:
        outcome = app.run(request)
    except ValidateInputError as exc:
        raise _bad_parameter(exc) from exc

    # Retention runs however emission ends. It is still ordered *after* the
    # summary (with --format all the sidecars are written into the render
    # dir), but a raise from _emit_result must not skip it and orphan the
    # rendered tree on disk.
    try:
        _emit_result(
            outcome,
            fmt=fmt,
            out_dir=outcome.out_dir,
            extra_warnings=outcome.warnings,
            requested_charts=request.charts,
            requested_environments=request.envs,
            timings=timings,
            verbose=request.verbose,
            github_step_summary=github_step_summary,
        )

        if fmt in ("text", "all"):
            _print_summary(outcome)
    finally:
        app.cleanup(outcome)
    sys.exit(outcome.exit_code)


def _bad_parameter(exc: ValidateInputError) -> typer.BadParameter:
    """Map a rejected service input onto the flag that carries it."""
    return typer.BadParameter(str(exc), param_hint=_PARAM_HINTS.get(exc.hint or "", exc.hint))


def _resolve_display(progress: str, *, fmt: str) -> ProgressDisplay:
    """Pick a display impl from the mode + format + TTY status.

    - none → NullDisplay.
    - plain → PlainNarrationDisplay (stderr lines).
    - live → LiveTableDisplay; falls back to plain if stderr isn't a TTY.
    - auto → live in interactive text mode, plain elsewhere.
    """
    if progress == "none":
        return NullDisplay()
    # Live table makes no sense alongside machine-readable output: the
    # JSON/markdown payload goes to stdout while the table renders on
    # stderr, which (a) confuses pipe consumers tee-ing both streams and
    # (b) silently masks any progress signal for downstream tooling. Drop
    # to the silent display so the contract is "machine output, no UI".
    if fmt in ("json", "md"):
        return NullDisplay()
    is_tty = sys.stderr.isatty()
    if progress == "plain":
        return PlainNarrationDisplay()
    if progress == "live":
        if not is_tty:
            narration.print(
                "[yellow]warn:[/yellow] --progress live requested but stderr is not a TTY; "
                "falling back to plain narration"
            )
            return PlainNarrationDisplay()
        return LiveTableDisplay()
    # auto
    if is_tty and fmt == "text":
        return LiveTableDisplay()
    return PlainNarrationDisplay() if fmt != "json" else NullDisplay()


def _emit_result(
    source: RunResult | RunOutcome,
    *,
    fmt: str,
    out_dir: Path,
    extra_warnings: tuple[str, ...] = (),
    requested_charts: tuple[str, ...] = (),
    requested_environments: tuple[str, ...] = (),
    timings: bool = False,
    verbose: bool = False,
    github_step_summary: bool = False,
) -> None:
    """Render a RunResult to stdout per `fmt` and side-emit summaries.

    Writes markdown to $GITHUB_STEP_SUMMARY only when the caller passes
    `github_step_summary=True` (driven by the `--github-step-summary`
    CLI flag). The presence of the env var alone is NOT sufficient —
    callers must opt in explicitly so local debugging on a runner-like
    shell never triggers a surprise side-channel write.

    For `fmt == "all"`, also writes <out_dir>/summary.md and
    <out_dir>/summary.json so post-job tooling can consume structured
    results without re-parsing markdown.
    """
    fmt = _validate_format(fmt)
    result = source.result if isinstance(source, RunOutcome) else source

    # Both projections are pure functions of (result, timings), and both have
    # more than one consumer: markdown feeds --format md, the `all` sidecar,
    # and the step summary; JSON feeds --format json and the sidecar. Memoize
    # rather than compute eagerly — the default `text` path needs neither, and
    # recomputing was the previous behavior (markdown up to 3x per run). The
    # caches are closures, so they die with this call; nothing is retained.
    @functools.cache
    def markdown_text() -> str:
        return to_markdown(
            source,
            include_timings=timings,
            requested_charts=requested_charts,
            requested_environments=requested_environments,
        )

    @functools.cache
    def json_text() -> str:
        return (
            json.dumps(
                to_json(
                    source,
                    requested_charts=requested_charts,
                    requested_environments=requested_environments,
                ),
                indent=2,
            )
            + "\n"
        )

    if fmt == "json":
        sys.stdout.write(json_text())
    elif fmt == "md":
        sys.stdout.write(markdown_text())
    else:  # text or all
        # The table and its detail blocks are the text projection.
        console.print(to_text_table(result, include_timings=timings))
        for block in failure_details(result):
            console.print(block)
        for block in advisory_details(result):
            console.print(block)
        # Operator warnings are not part of the projection.
        for warn in extra_warnings:
            narration.print(f"[yellow]warn:[/yellow] {warn}")

    if fmt == "all":
        # Best-effort: don't fail the run if the rendered tree was deleted.
        for filename, payload in (
            ("summary.md", markdown_text()),
            ("summary.json", json_text()),
        ):
            sidecar = out_dir / filename
            try:
                sidecar.parent.mkdir(parents=True, exist_ok=True)
                sidecar.write_text(payload)
            except OSError as exc:
                narration.print(f"[yellow]warning: could not write {sidecar}: {exc}[/yellow]")

    if github_step_summary:
        step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if not step_summary_path:
            # Warn rather than error: the flag is meant to assert intent,
            # but failing here makes local-with-flag debugging awkward and
            # would turn a side-channel emission into a fatal CLI error.
            narration.print(
                "[yellow]warning: --github-step-summary was passed but "
                "$GITHUB_STEP_SUMMARY is not set; skipping step summary write"
                "[/yellow]"
            )
        else:
            try:
                # GitHub aggregates step summaries: append, do not truncate.
                with open(step_summary_path, "a", encoding="utf-8") as fh:
                    fh.write(markdown_text())
            except OSError as exc:
                narration.print(
                    f"[yellow]warning: could not write GITHUB_STEP_SUMMARY ({exc})[/yellow]"
                )


def _parse_phases(raw: str) -> frozenset[str]:
    """Parse the comma-separated --phases value into a validated frozenset."""
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    if not parts:
        raise typer.BadParameter("--phases must list at least one phase", param_hint="--phases")
    unknown = parts - ALL_PHASES
    if unknown:
        raise typer.BadParameter(
            # PHASE_ORDER, not sorted(ALL_PHASES): show the phases in the
            # order the user would type them into --phases, which is also
            # the order the flag's default and help text use.
            f"unknown phase(s): {', '.join(sorted(unknown))}; valid: {','.join(PHASE_ORDER)}",
            param_hint="--phases",
        )
    return frozenset(parts)


def _print_summary(outcome: RunOutcome) -> None:
    """Print a one-line tally when any silent skips/errors are in play."""
    result = outcome.result
    bits: list[str] = []
    if result.spec_errors:
        bits.append(f"{len(result.spec_errors)} spec error(s)")
        for err in result.spec_errors:
            narration.print(f"[red]spec error:[/red] {err}")
    if outcome.charts_unvalidated:
        bits.append(f"{outcome.charts_unvalidated} chart(s) unvalidated")
    # Only a phase the caller *asked for* can be an anomaly worth reporting.
    # `--phases render` marks schema and policy NOT_RUN by design, so counting
    # every NOT_RUN made a deliberately narrowed run always print
    # "summary: 2 phase(s) NOT_RUN" — training the reader to ignore the one
    # line that exists to flag silent skips.
    not_run = sum(
        1
        for row in result.rows
        for name, phase in row.phases.items()
        if phase.status == "NOT_RUN" and name in outcome.enabled_phases
    )
    if not_run:
        bits.append(f"{not_run} phase(s) NOT_RUN")
    if not result.rows:
        bits.append("0 rows")
    if bits:
        narration.print(f"[bold]summary:[/bold] {'; '.join(bits)}")


def clean(
    root: RootOption = Path("."),
) -> None:
    """Remove the entire .chart-manager/rendered/ tree."""
    target = root.resolve() / ".chart-manager" / "rendered"
    if not target.exists():
        narration.print("nothing to clean")
        return
    try:
        shutil.rmtree(target)
    except OSError as exc:
        narration.print(f"[red]error:[/red] cleanup failed: {exc}")
        raise typer.Exit(1) from exc
    narration.print(f"cleaned: {target}")
