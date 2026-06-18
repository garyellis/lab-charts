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
from chart_manager.integrations.flux import Flux, HelmReleaseRef
from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.helmrelease import (
    HelmReleaseMatch,
    MonitorRequest,
    MonitorResult,
    MonitorService,
    PromoteRequest,
    PromoteService,
    TestRequest,
    TestResult,
    TestService,
    Transition,
)

_OUTPUT_CHOICES = ("pretty", "json", "auto")

ProgressCb = Callable[[HelmReleaseRef, Transition], None]


# --- factories (overrideable in tests) ------------------------------------


def _make_monitor_service(*, progress: ProgressCb | None) -> MonitorService:
    return MonitorService(flux=Flux(), progress=progress)


def _make_test_service(*, progress: ProgressCb | None) -> TestService:
    return TestService(flux=Flux(), helm=Helm(verbose=False), progress=progress)


# --- helpers --------------------------------------------------------------


def _resolve_output_mode(output: str, console: Console) -> Literal["pretty", "json"]:
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
    if mode == "json":
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)


def _coerce_namespace(ns: str | None) -> str | None:
    if ns is None or ns == "":
        return None
    return ns


def _make_console(no_color: bool) -> Console:
    return Console(no_color=no_color)


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
) -> None:
    """Wait for matched HelmReleases to converge on chart@version."""
    console = _make_console(no_color)
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
    )

    result = _run_monitor(mode, console, request)

    if mode == "pretty":
        render_monitor_pretty(result, console, chart=chart, version=version)
    else:
        render_monitor_json(result, sys.stdout, chart=chart, version=version)

    if not result.ok:
        raise typer.Exit(code=1)


def _run_monitor(
    mode: str,
    console: Console,
    request: MonitorRequest,
) -> MonitorResult:
    if mode == "pretty":
        with _PrettyProgressDriver(console, is_test=False) as driver:
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
) -> None:
    """Run `helm test` for matched HelmReleases and aggregate the verdict."""
    console = _make_console(no_color)
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
    )

    result = _run_test(mode, console, request)

    if mode == "pretty":
        render_test_pretty(result, console, chart=chart, version=version)
    else:
        render_test_json(result, sys.stdout, chart=chart, version=version)

    if not result.ok:
        raise typer.Exit(code=1)


def _run_test(
    mode: str,
    console: Console,
    request: TestRequest,
) -> TestResult:
    if mode == "pretty":
        with _PrettyProgressDriver(console, is_test=True) as driver:
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
    console = _make_console(no_color=False)

    def _confirm_downgrade(downgrades: list[HelmReleaseMatch], target: str) -> bool:
        console.print(
            f"[yellow]downgrade detected[/yellow]: target {target} is older than:"
        )
        for m in downgrades:
            ns = f"{m.namespace}/" if m.namespace else ""
            console.print(f"  - {ns}{m.name} ({m.path.name}): {m.current_version}")
        if allow_downgrade:
            console.print("[yellow]--allow-downgrade set; proceeding.[/yellow]")
            return True
        return typer.confirm("Proceed with the downgrade?", default=False)

    service = PromoteService(confirm_downgrade=_confirm_downgrade)
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

    if result.no_changes:
        count = len(result.matches)
        noun = "release" if count == 1 else "releases"
        console.print(
            f"[green]no changes[/green]: {count} {noun} already at {version} under {path}"
        )
        return

    if result.aborted:
        console.print("[yellow]aborted[/yellow]: declined downgrade prompt; no PR opened")
        return

    if result.already_open and result.pull_request is not None:
        console.print(
            f"[yellow]pr already open[/yellow]: {result.pull_request.url}"
        )
        return

    for changed in result.changed_files:
        console.print(f"updated [bold]{changed}[/bold]")

    if result.dry_run:
        console.print(f"[yellow]dry-run[/yellow] branch={result.branch}")
        return

    if result.pull_request is not None and result.pull_request.url:
        console.print(f"[green]pr opened[/green]: {result.pull_request.url}")
    else:
        console.print(f"[green]pushed[/green] branch={result.branch}")


def register(app: typer.Typer) -> None:
    app.command("promote")(promote)
    app.command("monitor")(monitor)
    app.command("test")(test)


__all__ = ["monitor", "promote", "register", "test"]
