"""`chart-manager helmrelease` subcommand handlers.

Thin CLI shell: argument shape, safety guard, output-mode resolution,
service construction (via overrideable factories), and renderer dispatch.
Business logic lives entirely in services/helmrelease.
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console

from chart_manager.cli.helmrelease_render import (
    _PrettyProgressDriver,
    render_monitor_json,
    render_monitor_pretty,
    render_test_json,
    render_test_pretty,
)
from chart_manager.cli.streams import data_console, narration_console
from chart_manager.composition import Container
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.helmrelease import (
    HelmReleaseMatch,
    HelmReleaseRef,
    MonitorRequest,
    MonitorResult,
    MonitorService,
    PromoteRequest,
    PromoteResult,
    PromoteService,
    PromoteStatus,
    TestRequest,
    TestResult,
    TestService,
    Transition,
)

_OUTPUT_CHOICES = ("pretty", "json", "auto")

ProgressCb = Callable[[HelmReleaseRef, Transition], None]


# --- factories (overrideable in tests) ------------------------------------
#
# Adapter wiring lives in `chart_manager.composition`; these stay as
# module-level functions purely as a test seam -- `tests/test_cli_helmrelease.py`
# monkeypatches them to inject fakes without touching the container.


def _container() -> Container:
    """Build the composition root for one CLI invocation."""
    return Container()


def _make_monitor_service(*, progress: ProgressCb | None) -> MonitorService:
    """Build the default MonitorService (module-level so tests can override)."""
    return _container().monitor_service(progress=progress)


def _make_test_service(*, progress: ProgressCb | None) -> TestService:
    """Build the default TestService (module-level so tests can override)."""
    return _container().test_service(progress=progress)


def _make_promote_service(
    *, confirm_downgrade: Callable[[list[HelmReleaseMatch], str], bool]
) -> PromoteService:
    """Build the default PromoteService (module-level so tests can override)."""
    return _container().promote_service(confirm_downgrade=confirm_downgrade)


# --- helpers --------------------------------------------------------------


def _resolve_output_mode(output: str, console: Console) -> Literal["pretty", "json"]:
    """Resolve --output: auto picks json when CI=true or stdout is not a terminal."""
    if output == "pretty":
        return "pretty"
    if output == "json":
        return "json"
    if output != "auto":
        raise ChartManagerError(
            f"--output must be one of {_OUTPUT_CHOICES} (got '{output}')"
        )
    if os.environ.get("CI") == "true":
        return "json"
    return "pretty" if console.is_terminal else "json"


def _setup_logging_for_mode(mode: str) -> None:
    """In json mode, route log records to stderr so stdout stays machine-parseable."""
    if mode == "json":
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)


def _coerce_namespace(ns: str | None) -> str | None:
    """Treat an empty --namespace string as None (meaning all namespaces)."""
    if ns is None or ns == "":
        return None
    return ns


def _make_console(no_color: bool) -> Console:
    """Console for the selected `--output` projection (stdout), honoring --no-color.

    Also the console `_resolve_output_mode` probes for `is_terminal`: the
    `auto` decision is "is the *data* going to a terminal", so it must ask
    about stdout, not about wherever narration happens to go.
    """
    return data_console(no_color=no_color)


def _make_narration_console(no_color: bool) -> Console:
    """Console for progress and status (stderr), honoring --no-color.

    Progress tables and promote's status lines are not the projection, so
    they belong on stderr regardless of `--output`. Previously they shared
    the stdout console and were safe only because json mode bypassed them;
    this makes the separation structural instead of mode-dependent.
    """
    return narration_console(no_color=no_color)


def _pr_url(result: PromoteResult) -> str:
    """The PR url for a status that carries one; empty is not reachable today."""
    return result.pull_request.url if result.pull_request is not None else ""


# --- command handlers -----------------------------------------------------


def monitor(
    chart: Annotated[str, typer.Option("--chart", help="chart name (Flux spec.chart.spec.chart)")],
    version: Annotated[str, typer.Option("--version", help="chart version to match")],
    namespace: Annotated[
        str | None, typer.Option("--namespace", help="limit to a single namespace (default: all)")
    ] = None,
    concurrency: Annotated[int, typer.Option("--concurrency", min=1, max=8)] = 4,
    per_poll_timeout: Annotated[str, typer.Option("--per-poll-timeout")] = "10s",
    per_hr_timeout: Annotated[str, typer.Option("--per-hr-timeout")] = "5m",
    total_timeout: Annotated[str, typer.Option("--total-timeout")] = "15m",
    output: Annotated[str, typer.Option("--output", help="pretty | json | auto")] = "auto",
    no_color: Annotated[bool, typer.Option("--no-color")] = False,
    fail_fast: Annotated[bool, typer.Option("--fail-fast")] = False,
    environment: Annotated[
        str | None,
        typer.Option(
            "--environment",
            help=(
                "promotion target this run belongs to; enables lifecycle "
                "events (omit for an ad-hoc run, which emits nothing)"
            ),
        ),
    ] = None,
) -> None:
    """Wait for matched HelmReleases to converge on chart@version."""
    console = _make_console(no_color)
    narration = _make_narration_console(no_color)
    mode = _resolve_output_mode(output, console)
    _setup_logging_for_mode(mode)

    request = MonitorRequest(
        chart_name=chart,
        version=version,
        namespace=_coerce_namespace(namespace),
        concurrency=concurrency,
        per_poll_timeout=per_poll_timeout,
        per_hr_timeout=per_hr_timeout,
        total_timeout=total_timeout,
        fail_fast=fail_fast,
        environment=environment,
    )

    result = _run_monitor(mode, narration, request)

    if mode == "pretty":
        render_monitor_pretty(result, console, chart=chart, version=version)
    else:
        render_monitor_json(result, sys.stdout, chart=chart, version=version)

    if not result.ok:
        raise typer.Exit(code=1)


def _run_monitor(
    mode: str,
    narration: Console,
    request: MonitorRequest,
) -> MonitorResult:
    """Run the monitor service, wiring a live progress table only in pretty mode.

    The driver renders onto the narration console: progress is never the
    selected projection.
    """
    if mode == "pretty":
        with _PrettyProgressDriver(narration) as driver:
            return _make_monitor_service(progress=driver).monitor(request)
    return _make_monitor_service(progress=None).monitor(request)


def test(
    chart: Annotated[str, typer.Option("--chart", help="chart name (Flux spec.chart.spec.chart)")],
    version: Annotated[str, typer.Option("--version", help="chart version to match")],
    namespace: Annotated[
        str | None, typer.Option("--namespace", help="limit to a single namespace (default: all)")
    ] = None,
    concurrency: Annotated[int, typer.Option("--concurrency", min=1, max=8)] = 4,
    per_poll_timeout: Annotated[str, typer.Option("--per-poll-timeout")] = "10s",
    per_hr_timeout: Annotated[str, typer.Option("--per-hr-timeout")] = "5m",
    total_timeout: Annotated[str, typer.Option("--total-timeout")] = "15m",
    output: Annotated[str, typer.Option("--output", help="pretty | json | auto")] = "auto",
    no_color: Annotated[bool, typer.Option("--no-color")] = False,
    pod_log_tail: Annotated[int, typer.Option("--pod-log-tail", min=1)] = 200,
    environment: Annotated[
        str | None,
        typer.Option(
            "--environment",
            help=(
                "promotion target this run belongs to; enables lifecycle "
                "events (omit for an ad-hoc run, which emits nothing)"
            ),
        ),
    ] = None,
) -> None:
    """Run `helm test` for matched HelmReleases and aggregate the verdict."""
    console = _make_console(no_color)
    narration = _make_narration_console(no_color)
    mode = _resolve_output_mode(output, console)
    _setup_logging_for_mode(mode)

    request = TestRequest(
        chart_name=chart,
        version=version,
        namespace=_coerce_namespace(namespace),
        concurrency=concurrency,
        per_poll_timeout=per_poll_timeout,
        per_hr_timeout=per_hr_timeout,
        total_timeout=total_timeout,
        pod_log_tail=pod_log_tail,
        environment=environment,
    )

    result = _run_test(mode, narration, request)

    if mode == "pretty":
        render_test_pretty(result, console, chart=chart, version=version)
    else:
        render_test_json(result, sys.stdout, chart=chart, version=version)

    if not result.ok:
        raise typer.Exit(code=1)


def _run_test(
    mode: str,
    narration: Console,
    request: TestRequest,
) -> TestResult:
    """Run the test service, wiring a live progress table only in pretty mode.

    The driver renders onto the narration console: progress is never the
    selected projection.
    """
    if mode == "pretty":
        with _PrettyProgressDriver(narration) as driver:
            return _make_test_service(progress=driver).test(request)
    return _make_test_service(progress=None).test(request)


def promote(
    flux_repo: Annotated[
        str,
        typer.Option(
            "--flux-repo",
            help="Upstream URL of the Flux GitOps repo (e.g. git@github.com:org/lab-fluxcd.git).",
        ),
    ],
    path: Annotated[
        Path,
        typer.Option(
            "--path",
            help="Directory within the flux repo to scan (e.g. 'prod/').",
        ),
    ],
    environment: Annotated[
        str,
        typer.Option("--environment", help="Environment label used in branch / PR text."),
    ],
    chart_name: Annotated[
        str,
        typer.Option("--chart-name", help="HelmRelease .spec.chart.spec.chart value to match."),
    ],
    version: Annotated[
        str,
        typer.Option("--version", help="Target chart version to set."),
    ],
    base_branch: Annotated[
        str,
        typer.Option("--base-branch", help="Base branch the PR targets."),
    ] = "main",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the planned PR (files, branch, title); no edits, no push."),
    ] = False,
    allow_downgrade: Annotated[
        bool,
        typer.Option(
            "--allow-downgrade",
            help="Proceed without prompting when target version is older than what's currently in the file.",
        ),
    ] = False,
) -> None:
    """Open a PR in the flux repo that bumps a chart's version in a target environment."""
    # `promote` has no `--output` projection yet (P0.3 adds one), so every
    # line below is narration and goes to stderr. When the json projection
    # lands it writes to a `data_console()` and none of this moves.
    narration = _make_narration_console(no_color=False)

    def _confirm_downgrade(downgrades: list[HelmReleaseMatch], target: str) -> bool:
        """Prompt to proceed on a detected downgrade; auto-yes when --allow-downgrade is set."""
        narration.print(
            f"[yellow]downgrade detected[/yellow]: target {target} is older than:"
        )
        for m in downgrades:
            ns = f"{m.namespace}/" if m.namespace else ""
            narration.print(f"  - {ns}{m.name} ({m.path.name}): {m.current_version}")
        if allow_downgrade:
            narration.print("[yellow]--allow-downgrade set; proceeding.[/yellow]")
            return True
        return typer.confirm("Proceed with the downgrade?", default=False)

    service = _make_promote_service(confirm_downgrade=_confirm_downgrade)
    result = service.promote(
        PromoteRequest(
            flux_repo=flux_repo,
            path=path,
            environment=environment,
            chart_name=chart_name,
            version=version,
            base_branch=base_branch,
            dry_run=dry_run,
        )
    )

    # The three states that used to return before this loop leave
    # `changed_files` empty by construction, so hoisting it is print-identical
    # and lets the status be decoded exactly once, in one exhaustive match.
    for changed in result.changed_files:
        narration.print(f"updated [bold]{changed}[/bold]")

    match result.status:
        case PromoteStatus.NO_CHANGES:
            count = len(result.matches)
            noun = "release" if count == 1 else "releases"
            narration.print(
                f"[green]no changes[/green]: {count} {noun} already at {version} under {path}"
            )
        case PromoteStatus.ABORTED:
            narration.print(
                "[yellow]aborted[/yellow]: declined downgrade prompt; no PR opened"
            )
        case PromoteStatus.ALREADY_OPEN:
            # The service pairs this status with the existing PR. The old
            # `already_open and result.pull_request is not None` guard let the
            # impossible pair fall through to the "pushed branch=..." line,
            # which tells the operator the opposite of what happened.
            narration.print(f"[yellow]pr already open[/yellow]: {_pr_url(result)}")
        case PromoteStatus.DRY_RUN:
            narration.print(f"[yellow]dry-run[/yellow] branch={result.branch}")
        case PromoteStatus.PR_OPENED:
            narration.print(f"[green]pr opened[/green]: {_pr_url(result)}")
        case PromoteStatus.PUSHED:
            narration.print(f"[green]pushed[/green] branch={result.branch}")


def register(app: typer.Typer) -> None:
    """Attach the helmrelease subcommands to the given Typer app."""
    app.command("promote")(promote)
    app.command("monitor")(monitor)
    app.command("test")(test)


__all__ = ["monitor", "promote", "register", "test"]
