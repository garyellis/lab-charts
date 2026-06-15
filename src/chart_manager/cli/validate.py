"""`chart-manager validate` sub-app.

Commands register themselves onto a Typer app passed in by cli/main.py.
This `register(app)` pattern keeps cli/main.py free of validate-specific
imports and lets the sub-app grow (render/schema/policy/run/clean/deps-install)
without touching main.py.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from chart_manager.integrations.git import Git
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.integrations.kyverno import Kyverno
from chart_manager.plumbing.charts import ChartRepository
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ChartManagerError, ChartNotFoundError
from chart_manager.plumbing.validate_models import RowResult, RunResult, WorklistRow
from chart_manager.plumbing.validate_spec import ValidateSpec
from chart_manager.services.validate import deps_install as deps_install_mod
from chart_manager.services.validate.progress import (
    LiveTableDisplay,
    NullDisplay,
    PlainNarrationDisplay,
    ProgressDisplay,
)
from chart_manager.services.validate.rendering import (
    advisory_details,
    failure_details,
    to_json,
    to_markdown,
    to_text_table,
)
from chart_manager.services.validate.runner import RowConfig, ValidateRunner
from chart_manager.services.validate.worklist import (
    WorklistBuildResult,
    build_single_row,
    build_worklist,
    discover_policies,
)

_FORMATS = ("text", "md", "json", "all")
_PROGRESS_MODES = ("auto", "live", "plain", "none")
FormatOption = Annotated[
    str,
    typer.Option(
        "--format",
        help="Output format: text (default), md, json, or all.",
    ),
]


def _default_workers() -> int:
    cpu = os.cpu_count() or 2
    return max(2, min(cpu, 8))

console = Console()


def register(app: typer.Typer) -> None:
    app.command("render")(render)
    app.command("schema")(schema)
    app.command("policy")(policy)
    app.command("run")(run)
    app.command("clean")(clean)
    app.command("deps-install")(deps_install)


def render(
    chart: Annotated[str, typer.Option("--chart", help="Chart name (resolved under <root>/charts/) or path containing '/'.")],
    env: Annotated[
        str,
        typer.Option(
            "--env",
            help=(
                "Environment label. Used for the namespace default (lab-<env>) and output path. "
                "Single-row commands (render/schema/policy) do NOT consult validate-spec.yaml — "
                "pass --values explicitly to overlay per-env values. Use `validate run` for "
                "spec-driven multi-row execution."
            ),
        ),
    ],
    values: Annotated[
        list[Path],
        typer.Option(
            "--values",
            help=(
                "Values file (repeatable, applied in order). "
                "Defaults to <chart>/values.yaml only if no --values flags are passed."
            ),
        ),
    ] = [],
    namespace: Annotated[
        str | None,
        typer.Option("--namespace", help="Kubernetes namespace. Defaults to lab-<env>."),
    ] = None,
    release: Annotated[
        str | None,
        typer.Option("--release", help="Helm release name. Defaults to the chart name."),
    ] = None,
    helm_version: Annotated[
        str | None,
        typer.Option("--helm-version", help="Resolve helm via `mise where helm@<version>`."),
    ] = None,
    helm_bin: Annotated[
        Path | None,
        typer.Option("--helm-bin", help="Explicit path to a helm binary."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Render output dir. Defaults to <root>/.chart-manager/rendered/<run-id>/."),
    ] = None,
    keep: Annotated[
        bool,
        typer.Option("--keep/--no-keep", help="Keep rendered output on success."),
    ] = False,
    fmt: FormatOption = "text",
    root: Annotated[Path, typer.Option("--root", help="Repository root.")] = Path("."),
) -> None:
    """Render one chart x env via `helm template` and print a status row."""
    if helm_version is not None and helm_bin is not None:
        raise typer.BadParameter(
            "--helm-version and --helm-bin are mutually exclusive",
            param_hint="--helm-version / --helm-bin",
        )

    repo_root = root.resolve()
    chart_path, chart_label = _resolve_chart_path(repo_root, chart)
    resolved_values = _resolve_values(chart_path, values)
    resolved_namespace = namespace or f"lab-{env}"
    resolved_release = release or chart_label

    # An explicit --out means the user named the directory deliberately; treat
    # that as an implicit --keep so we don't surprise-delete their target.
    user_specified_out = out is not None
    out_dir = out.resolve() if out is not None else _default_out_dir(repo_root)
    effective_keep = keep or user_specified_out

    runner_cmd = CommandRunner()
    helm = Helm(runner=runner_cmd, version=helm_version, binary=helm_bin)
    runner = ValidateRunner(helm=helm, output_root=out_dir)

    row = build_single_row(
        chart=chart_label,
        env=env,
        namespace=resolved_namespace,
        release=resolved_release,
    )
    configs = [RowConfig(row=row, chart_path=chart_path, values=resolved_values)]

    result = runner.run(configs)

    _emit_result(result, fmt=fmt, out_dir=out_dir)

    exit_code = result.exit_code()
    _maybe_cleanup(out_dir, exit_code=exit_code, keep=effective_keep)
    sys.exit(exit_code)


def schema(
    chart: Annotated[str, typer.Option("--chart", help="Chart name (resolved under <root>/charts/) or path containing '/'.")],
    env: Annotated[
        str,
        typer.Option(
            "--env",
            help=(
                "Environment label. Used for the namespace default (lab-<env>) and output path. "
                "Single-row commands (render/schema/policy) do NOT consult validate-spec.yaml — "
                "pass --values explicitly to overlay per-env values. Use `validate run` for "
                "spec-driven multi-row execution."
            ),
        ),
    ],
    values: Annotated[
        list[Path],
        typer.Option(
            "--values",
            help=(
                "Values file (repeatable, applied in order). "
                "Defaults to <chart>/values.yaml only if no --values flags are passed."
            ),
        ),
    ] = [],
    namespace: Annotated[
        str | None,
        typer.Option("--namespace", help="Kubernetes namespace. Defaults to lab-<env>."),
    ] = None,
    release: Annotated[
        str | None,
        typer.Option("--release", help="Helm release name. Defaults to the chart name."),
    ] = None,
    helm_version: Annotated[
        str | None,
        typer.Option("--helm-version", help="Resolve helm via `mise where helm@<version>`."),
    ] = None,
    helm_bin: Annotated[
        Path | None,
        typer.Option("--helm-bin", help="Explicit path to a helm binary."),
    ] = None,
    kube_version: Annotated[
        str | None,
        typer.Option(
            "--kube-version",
            help=(
                "Kubernetes version for kubeconform (e.g. 1.31.2). "
                "Defaults to kubeconform's built-in default. "
                "For multi-row runs this is sourced from validate-spec.yaml (kubernetes_version)."
            ),
        ),
    ] = None,
    schema_location: Annotated[
        list[str],
        typer.Option(
            "--schema-location",
            help=(
                "Override kubeconform schema search path (repeatable). "
                "Default: ['default', datreeio CRDs catalog]. "
                "For multi-row runs this is sourced from validate-spec.yaml (schema_locations)."
            ),
        ),
    ] = [],
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Render output dir. Defaults to <root>/.chart-manager/rendered/<run-id>/."),
    ] = None,
    keep: Annotated[
        bool,
        typer.Option("--keep/--no-keep", help="Keep rendered output on success."),
    ] = False,
    fmt: FormatOption = "text",
    root: Annotated[Path, typer.Option("--root", help="Repository root.")] = Path("."),
) -> None:
    """Render one chart x env then validate manifests with kubeconform."""
    if helm_version is not None and helm_bin is not None:
        raise typer.BadParameter(
            "--helm-version and --helm-bin are mutually exclusive",
            param_hint="--helm-version / --helm-bin",
        )

    repo_root = root.resolve()
    chart_path, chart_label = _resolve_chart_path(repo_root, chart)
    resolved_values = _resolve_values(chart_path, values)
    resolved_namespace = namespace or f"lab-{env}"
    resolved_release = release or chart_label

    user_specified_out = out is not None
    out_dir = out.resolve() if out is not None else _default_out_dir(repo_root)
    effective_keep = keep or user_specified_out

    runner_cmd = CommandRunner()
    helm = Helm(runner=runner_cmd, version=helm_version, binary=helm_bin)
    kubeconform = Kubeconform(runner=runner_cmd)
    runner = ValidateRunner(
        helm=helm,
        output_root=out_dir,
        kubeconform=kubeconform,
    )

    row = build_single_row(
        chart=chart_label,
        env=env,
        namespace=resolved_namespace,
        release=resolved_release,
    )
    configs = [
        RowConfig(
            row=row,
            chart_path=chart_path,
            values=resolved_values,
            kubernetes_version=kube_version,
            schema_locations=list(schema_location) if schema_location else None,
        )
    ]

    result = runner.run(configs)

    _emit_result(result, fmt=fmt, out_dir=out_dir)

    exit_code = result.exit_code()
    _maybe_cleanup(out_dir, exit_code=exit_code, keep=effective_keep)
    sys.exit(exit_code)


def policy(
    chart: Annotated[str, typer.Option("--chart", help="Chart name (resolved under <root>/charts/) or path containing '/'.")],
    env: Annotated[
        str,
        typer.Option(
            "--env",
            help=(
                "Environment label. Used for the namespace default (lab-<env>) and output path. "
                "Single-row commands (render/schema/policy) do NOT consult validate-spec.yaml — "
                "pass --values explicitly to overlay per-env values. Use `validate run` for "
                "spec-driven multi-row execution."
            ),
        ),
    ],
    values: Annotated[
        list[Path],
        typer.Option(
            "--values",
            help=(
                "Values file (repeatable, applied in order). "
                "Defaults to <chart>/values.yaml only if no --values flags are passed."
            ),
        ),
    ] = [],
    namespace: Annotated[
        str | None,
        typer.Option("--namespace", help="Kubernetes namespace. Defaults to lab-<env>."),
    ] = None,
    release: Annotated[
        str | None,
        typer.Option("--release", help="Helm release name. Defaults to the chart name."),
    ] = None,
    helm_version: Annotated[
        str | None,
        typer.Option("--helm-version", help="Resolve helm via `mise where helm@<version>`."),
    ] = None,
    helm_bin: Annotated[
        Path | None,
        typer.Option("--helm-bin", help="Explicit path to a helm binary."),
    ] = None,
    kube_version: Annotated[
        str | None,
        typer.Option(
            "--kube-version",
            help="Kubernetes version for kubeconform (e.g. 1.31.2). Multi-row runs source this from validate-spec.yaml.",
        ),
    ] = None,
    schema_location: Annotated[
        list[str],
        typer.Option(
            "--schema-location",
            help="Override kubeconform schema search path (repeatable). Multi-row runs source this from validate-spec.yaml.",
        ),
    ] = [],
    policy_dir: Annotated[
        list[Path],
        typer.Option(
            "--policy-dir",
            help=(
                "Kyverno policy directory (repeatable). Defaults to "
                "<root>/policies and <root>/charts/<chart>/policies "
                "(whichever exist)."
            ),
        ),
    ] = [],
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Render output dir. Defaults to <root>/.chart-manager/rendered/<run-id>/."),
    ] = None,
    keep: Annotated[
        bool,
        typer.Option("--keep/--no-keep", help="Keep rendered output on success."),
    ] = False,
    fmt: FormatOption = "text",
    root: Annotated[Path, typer.Option("--root", help="Repository root.")] = Path("."),
) -> None:
    """Render -> schema -> policy for one chart x env via kyverno."""
    if helm_version is not None and helm_bin is not None:
        raise typer.BadParameter(
            "--helm-version and --helm-bin are mutually exclusive",
            param_hint="--helm-version / --helm-bin",
        )

    repo_root = root.resolve()
    chart_path, chart_label = _resolve_chart_path(repo_root, chart)
    resolved_values = _resolve_values(chart_path, values)
    resolved_namespace = namespace or f"lab-{env}"
    resolved_release = release or chart_label

    user_specified_out = out is not None
    out_dir = out.resolve() if out is not None else _default_out_dir(repo_root)
    effective_keep = keep or user_specified_out

    # Explicit flags override discovery; empty list falls back to defaults.
    if policy_dir:
        resolved_policy_paths = [
            p if p.is_absolute() else (repo_root / p).resolve() for p in policy_dir
        ]
    else:
        resolved_policy_paths = discover_policies(repo_root, chart_label)

    runner_cmd = CommandRunner()
    helm = Helm(runner=runner_cmd, version=helm_version, binary=helm_bin)
    kubeconform = Kubeconform(runner=runner_cmd)
    kyverno = Kyverno(runner=runner_cmd)
    runner = ValidateRunner(
        helm=helm,
        output_root=out_dir,
        kubeconform=kubeconform,
        kyverno=kyverno,
    )

    row = build_single_row(
        chart=chart_label,
        env=env,
        namespace=resolved_namespace,
        release=resolved_release,
    )
    configs = [
        RowConfig(
            row=row,
            chart_path=chart_path,
            values=resolved_values,
            kubernetes_version=kube_version,
            schema_locations=list(schema_location) if schema_location else None,
            policy_paths=resolved_policy_paths,
        )
    ]

    result = runner.run(configs)

    _emit_result(result, fmt=fmt, out_dir=out_dir)

    exit_code = result.exit_code()
    _maybe_cleanup(out_dir, exit_code=exit_code, keep=effective_keep)
    sys.exit(exit_code)


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
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Render output dir. Defaults to <root>/.chart-manager/rendered/<run-id>/."),
    ] = None,
    keep: Annotated[
        bool,
        typer.Option("--keep/--no-keep", help="Keep rendered output on success."),
    ] = False,
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
                "Progress UI: auto (default; live in TTY+text, plain "
                "otherwise), live, plain, none."
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
    fmt: FormatOption = "text",
    root: Annotated[Path, typer.Option("--root", help="Repository root.")] = Path("."),
) -> None:
    """Build worklist from validate-spec.yaml + git, then run all phases.

    Source of the changed-files list (in precedence order):
      --all > --changed-files > git diff against --base.
    """
    enabled_phases = _parse_phases(phases)
    if progress not in _PROGRESS_MODES:
        raise typer.BadParameter(
            f"unknown progress mode: {progress} (allowed: {', '.join(_PROGRESS_MODES)})",
            param_hint="--progress",
        )

    repo_root = root.resolve()
    chart_filter = set(chart)
    env_filter = set(env)

    changed_files_list = _resolve_changed_files(
        repo_root,
        all_charts=all_charts,
        changed_files_path=changed_files,
        base=base,
    )

    build = build_worklist(
        root=repo_root,
        changed_files=changed_files_list,
        all_charts=all_charts,
    )

    rows = build.rows
    if chart_filter:
        rows = tuple(r for r in rows if r.chart in chart_filter)
    if env_filter:
        rows = tuple(r for r in rows if r.env in env_filter)

    # Output dir: an explicit --out is treated as implicit --keep so we
    # don't surprise-delete a user-named directory.
    user_specified_out = out is not None
    out_dir = out.resolve() if out is not None else _default_out_dir(repo_root)
    effective_keep = keep or user_specified_out

    runner_cmd = CommandRunner()

    # --verbose forces plain progress (Live can't share the terminal with
    # streaming subprocess output) and disables capture in Helm so the
    # operator sees raw helm chatter.
    helm_verbose = bool(verbose)
    if verbose and progress in ("auto", "live"):
        progress = "plain"

    # Specs may pin a helm version per chart; group rows by their helm
    # binding so we don't construct N runners with the same defaults.
    helm_default = Helm(runner=runner_cmd, verbose=helm_verbose)
    kubeconform = Kubeconform(runner=runner_cmd)
    kyverno = Kyverno(runner=runner_cmd)

    grouped: dict[tuple[str | None, str | None], list[RowConfig]] = {}
    for row in rows:
        spec = build.specs.get(row.chart)
        if spec is None:
            continue
        cfg = _row_config_for(repo_root, row, spec)
        key = (spec.helm_version, spec.helm_bin)
        grouped.setdefault(key, []).append(cfg)

    resolved_workers = _default_workers() if workers == 0 else max(1, workers)
    # --verbose streams raw subprocess stdout/stderr; with >1 worker those
    # streams interleave into illegible noise and defeat the point of
    # --verbose (which exists for debugging hangs). Force serial and warn
    # rather than silently producing garbled output.
    if verbose and resolved_workers > 1:
        console.print(
            "[yellow]warn:[/yellow] --verbose forces --workers=1 to keep "
            "streamed subprocess output readable"
        )
        resolved_workers = 1
    all_cfgs: list[RowConfig] = [c for cfgs in grouped.values() for c in cfgs]
    display = _resolve_display(progress, fmt=fmt)
    # The display API takes WorklistRow (not RowConfig) so the progress
    # module has no upward dependency on the runner package.
    display.start([cfg.row for cfg in all_cfgs])
    on_event = display.on_event

    aggregated_rows: list[RowResult] = []
    try:
        for (helm_version, helm_bin), cfgs in grouped.items():
            helm = (
                helm_default
                if helm_version is None and helm_bin is None
                else Helm(
                    runner=runner_cmd,
                    version=helm_version,
                    binary=helm_bin,
                    verbose=helm_verbose,
                )
            )
            runner_inst = ValidateRunner(
                helm=helm,
                output_root=out_dir,
                kubeconform=kubeconform,
                kyverno=kyverno,
                max_workers=resolved_workers,
                on_event=on_event,
                # 0 means unbounded per CLI semantics; convert to None at the
                # runner/integration boundary so subprocess.run gets the
                # right sentinel.
                row_timeout=row_timeout if row_timeout > 0 else None,
                dep_update_timeout=dep_update_timeout if dep_update_timeout > 0 else None,
            )
            sub = runner_inst.run(cfgs, enabled_phases=enabled_phases)
            aggregated_rows.extend(sub.rows)
    finally:
        display.stop()

    # Re-sort the aggregated rows so output is deterministic even across
    # helm-binding groups.
    aggregated_rows.sort(key=lambda r: (r.row.chart, r.row.env))

    result = RunResult(
        rows=tuple(aggregated_rows),
        rendered_root=out_dir,
        spec_errors=build.spec_errors,
    )

    _emit_result(
        result,
        fmt=fmt,
        out_dir=out_dir,
        extra_warnings=tuple(build.warnings),
        timings=timings,
        verbose=verbose,
    )

    if fmt in ("text", "all"):
        _print_summary(result, build, enabled_phases)

    exit_code = result.exit_code()
    _maybe_cleanup(out_dir, exit_code=exit_code, keep=effective_keep)
    sys.exit(exit_code)


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
            console.print(
                "[yellow]warn:[/yellow] --progress live requested but stderr is not a TTY; "
                "falling back to plain narration"
            )
            return PlainNarrationDisplay()
        return LiveTableDisplay()
    # auto
    if is_tty and fmt == "text":
        return LiveTableDisplay()
    return PlainNarrationDisplay() if fmt != "json" else NullDisplay()


def _validate_format(value: str) -> str:
    if value not in _FORMATS:
        raise typer.BadParameter(
            f"unknown format: {value} (allowed: {', '.join(_FORMATS)})",
            param_hint="--format",
        )
    return value


def _emit_result(
    result: RunResult,
    *,
    fmt: str,
    out_dir: Path,
    extra_warnings: tuple[str, ...] = (),
    timings: bool = False,
    verbose: bool = False,
) -> None:
    """Render a RunResult to stdout per `fmt` and side-emit summaries.

    Always writes markdown to $GITHUB_STEP_SUMMARY when set, regardless
    of `fmt`. For `fmt == "all"`, also writes <out_dir>/summary.md and
    <out_dir>/summary.json so post-job tooling can consume structured
    results without re-parsing markdown.
    """
    fmt = _validate_format(fmt)

    if fmt == "json":
        sys.stdout.write(json.dumps(to_json(result, include_timings=timings), indent=2) + "\n")
    elif fmt == "md":
        sys.stdout.write(to_markdown(result, include_timings=timings))
    else:  # text or all
        console.print(to_text_table(result, include_timings=timings))
        for block in failure_details(result):
            console.print(block)
        for block in advisory_details(result):
            console.print(block)
        for warn in extra_warnings:
            console.print(f"[yellow]warn:[/yellow] {warn}")

    if fmt == "all":
        # Best-effort: don't fail the run if the rendered tree was deleted.
        for filename, payload in (
            ("summary.md", to_markdown(result, include_timings=timings)),
            ("summary.json", json.dumps(to_json(result, include_timings=timings), indent=2) + "\n"),
        ):
            sidecar = out_dir / filename
            try:
                sidecar.parent.mkdir(parents=True, exist_ok=True)
                sidecar.write_text(payload)
            except OSError as exc:
                console.print(f"[yellow]warning: could not write {sidecar}: {exc}[/yellow]")

    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        try:
            # GitHub aggregates step summaries: append, do not truncate.
            with open(step_summary_path, "a", encoding="utf-8") as fh:
                fh.write(to_markdown(result, include_timings=timings))
        except OSError as exc:
            console.print(
                f"[yellow]warning: could not write GITHUB_STEP_SUMMARY ({exc})[/yellow]"
            )


def _parse_phases(raw: str) -> frozenset[str]:
    valid = {"render", "schema", "policy"}
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    if not parts:
        raise typer.BadParameter("--phases must list at least one phase", param_hint="--phases")
    unknown = parts - valid
    if unknown:
        raise typer.BadParameter(
            f"unknown phase(s): {', '.join(sorted(unknown))}; valid: render,schema,policy",
            param_hint="--phases",
        )
    return frozenset(parts)


def _resolve_changed_files(
    repo_root: Path,
    *,
    all_charts: bool,
    changed_files_path: Path | None,
    base: str,
) -> list[str] | None:
    if all_charts:
        return None
    if changed_files_path is not None:
        try:
            text = changed_files_path.read_text()
        except OSError as exc:
            raise typer.BadParameter(
                f"cannot read --changed-files: {exc}", param_hint="--changed-files"
            ) from exc
        return [line for line in text.splitlines() if line.strip()]
    git = Git(repo_root)
    try:
        return git.changed_files(base=base)
    except ChartManagerError as exc:
        console.print(f"[yellow]warn:[/yellow] git diff failed ({exc}); falling back to --all")
        return None


def _row_config_for(
    repo_root: Path,
    row: WorklistRow,
    spec: ValidateSpec,
) -> RowConfig:
    chart_path = (repo_root / "charts" / row.chart).resolve()
    env_spec = spec.environments[row.env]
    values = [
        (chart_path / v).resolve() for v in env_spec.values
    ]
    policy_paths = list(discover_policies(repo_root, row.chart))
    for extra in spec.policies.extra:
        extra_path = Path(extra)
        if not extra_path.is_absolute():
            extra_path = (repo_root / extra).resolve()
        if extra_path.is_dir() and extra_path not in policy_paths:
            policy_paths.append(extra_path)
    return RowConfig(
        row=row,
        chart_path=chart_path,
        values=values,
        kubernetes_version=spec.kubernetes_version,
        schema_locations=spec.schema_locations or None,
        policy_paths=policy_paths,
    )


def _print_summary(
    result: RunResult,
    build: WorklistBuildResult,
    enabled_phases: frozenset[str],
) -> None:
    """Print a one-line tally when any silent skips/errors are in play."""
    bits: list[str] = []
    if build.spec_errors:
        bits.append(f"{len(build.spec_errors)} spec error(s)")
        for err in build.spec_errors:
            console.print(f"[red]spec error:[/red] {err}")
    if build.chart_count_unvalidated:
        bits.append(f"{build.chart_count_unvalidated} chart(s) unvalidated")
    not_run = sum(
        1
        for row in result.rows
        for phase in row.phases.values()
        if phase.status == "NOT_RUN"
    )
    if not_run:
        bits.append(f"{not_run} phase(s) NOT_RUN")
    if not result.rows:
        bits.append("0 rows")
    if bits:
        console.print(f"[bold]summary:[/bold] {'; '.join(bits)}")


def clean(
    root: Annotated[Path, typer.Option("--root", help="Repository root.")] = Path("."),
) -> None:
    """Remove the entire .chart-manager/rendered/ tree."""
    target = (root.resolve() / ".chart-manager" / "rendered")
    if not target.exists():
        console.print("nothing to clean")
        return
    try:
        shutil.rmtree(target)
    except OSError as exc:
        console.print(f"[red]error:[/red] cleanup failed: {exc}")
        raise typer.Exit(1) from exc
    console.print(f"cleaned: {target}")


# Derived from the service registry so adding a tool there automatically
# extends the CLI's allow-list — no second source of truth to drift.
_DEPS_INSTALL_TOOLS = deps_install_mod.KNOWN_TOOLS


def deps_install(
    tool: Annotated[
        list[str],
        typer.Option(
            "--tool",
            help=(
                "Tool(s) to install (repeatable). Allowed: "
                f"{', '.join(_DEPS_INSTALL_TOOLS)}. "
                "Defaults to --all when omitted."
            ),
        ),
    ] = [],
    all_tools: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Install every known tool. Mutually exclusive with --tool.",
        ),
    ] = False,
) -> None:
    """Install validate pipeline tool versions via mise.

    Per-tool failures downgrade to warnings (with upstream release URLs)
    but still flip the exit code so CI surfaces a partial install rather
    than passing on soft warnings.
    """
    if tool and all_tools:
        raise typer.BadParameter(
            "--tool and --all are mutually exclusive",
            param_hint="--tool / --all",
        )
    unknown = [t for t in tool if t not in _DEPS_INSTALL_TOOLS]
    if unknown:
        raise typer.BadParameter(
            f"unknown tool(s): {', '.join(unknown)} "
            f"(allowed: {', '.join(_DEPS_INSTALL_TOOLS)})",
            param_hint="--tool",
        )

    runner_cmd = CommandRunner()
    if all_tools or not tool:
        results = deps_install_mod.install_all(runner_cmd, on_warn=console.print)
    else:
        results = []
        for t in tool:
            results.extend(
                deps_install_mod.install_one(runner_cmd, t, on_warn=console.print)
            )

    for r in results:
        status = "ok" if r.success else "failed"
        suffix = ""
        if not r.success:
            url = deps_install_mod.release_url(r.tool, r.version)
            suffix = f" (release: {url})"
        console.print(f"{r.tool}@{r.version}: {status}{suffix}")

    failed = sum(1 for r in results if not r.success)
    total = len(results)
    console.print(f"summary: {total - failed}/{total} installed")
    if failed:
        raise typer.Exit(1)


def _resolve_chart_path(repo_root: Path, chart_arg: str) -> tuple[Path, str]:
    """Resolve a --chart argument into (absolute chart dir, display label).

    A bare name is resolved through ChartRepository. A value containing
    '/' is treated as a path (relative to repo root, or absolute) — this
    is the M1b escape hatch for fixture charts that live outside charts/.
    """
    if "/" in chart_arg:
        candidate = Path(chart_arg)
        if not candidate.is_absolute():
            candidate = (repo_root / candidate).resolve()
        if not (candidate / "Chart.yaml").is_file():
            raise typer.BadParameter(
                f"no Chart.yaml at {candidate}", param_hint="--chart"
            )
        return candidate, candidate.name

    repo = ChartRepository(repo_root)
    try:
        chart_obj = repo.get(chart_arg)
    except ChartNotFoundError as exc:
        raise typer.BadParameter(str(exc), param_hint="--chart") from exc
    return chart_obj.path, chart_obj.name


def _resolve_values(chart_path: Path, values: list[Path]) -> list[Path]:
    if values:
        resolved: list[Path] = []
        for value in values:
            resolved.append(value if value.is_absolute() else (chart_path / value).resolve())
        return resolved
    default = chart_path / "values.yaml"
    return [default.resolve()] if default.is_file() else []


def _default_out_dir(repo_root: Path) -> Path:
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    return (repo_root / ".chart-manager" / "rendered" / run_id).resolve()


def _maybe_cleanup(out_dir: Path, *, exit_code: int, keep: bool) -> None:
    # Keep on: --keep, any failure, or DEBUG=true. Never crash cleanup.
    if keep or exit_code != 0 or os.environ.get("DEBUG", "").lower() == "true":
        return
    if not out_dir.exists():
        return
    try:
        shutil.rmtree(out_dir)
    except OSError as exc:
        console.print(f"[yellow]warning: cleanup failed: {exc}[/yellow]")
