"""`chart-manager lifecycle` command surface.

The lifecycle service owns compilation, diagnostics, evidence matching, and
freshness.  This module only validates CLI vocabulary, selects a service
operation, and renders its structured result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import uuid4

import typer
import yaml
from rich.console import Console
from rich.table import Table

from chart_manager.composition import Container, Settings
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.clusters.ephemeral import DEFAULT_CLUSTER_NAME
from chart_manager.services.lifecycle import LifecycleCompiler, doctor_lifecycle
from chart_manager.services.lifecycle.evidence import ClusterIdentity, LocalEvidenceRepository
from chart_manager.services.lifecycle.impact import LifecycleImpactService
from chart_manager.services.lifecycle.observers import (
    HelmReleaseReader,
    HelmReleaseStatusObserver,
    WorkloadReadinessObserver,
)
from chart_manager.services.lifecycle.status import LifecycleStatusService

_WORKFLOWS = ("validation", "cluster-test")
_FORMATS = ("text", "json", "yaml")

console = Console()

RootOption = Annotated[Path, typer.Option("--root", help="Repository root.")]
WorkflowOption = Annotated[
    str,
    typer.Option(
        "--workflow",
        help="Lifecycle workflow: validation or cluster-test.",
        callback=lambda value: _choice(value, _WORKFLOWS, "--workflow"),
    ),
]
ProfileOption = Annotated[
    str,
    typer.Option(
        "--profile",
        help="Validation environment or cluster-test profile.",
    ),
]
FormatOption = Annotated[
    str,
    typer.Option(
        "--format",
        help="Output format: text, json, or yaml.",
        callback=lambda value: _choice(value, _FORMATS, "--format"),
    ),
]


def register(app: typer.Typer) -> None:
    """Attach lifecycle commands to ``app``."""

    app.command("plan")(plan)
    app.command("doctor")(doctor)
    app.command("status")(status)
    app.command("impact")(impact)


def _choice(value: str, allowed: tuple[str, ...], option: str) -> str:
    if value not in allowed:
        raise typer.BadParameter(
            f"unknown value: {value} (allowed: {', '.join(allowed)})",
            param_hint=option,
        )
    return value


def _compiler(root: Path) -> LifecycleCompiler:
    """Build the compiler at a seam tests and future composition can replace."""

    return LifecycleCompiler(root, charts_dir=Settings().charts_dir)


def _compile(root: Path, chart: str, workflow: str, profile: str) -> Any:
    compiler = _compiler(root)
    if workflow == "validation":
        return compiler.compile_validation(chart, profile)
    return compiler.compile_cluster_test(chart, profile)


def _json(data: dict[str, Any]) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True))


def _yaml(data: dict[str, Any]) -> None:
    typer.echo(yaml.safe_dump(data, sort_keys=False), nl=False)


def _action_value(action: dict[str, Any], *names: str, default: str = "") -> str:
    for name in names:
        value = action.get(name)
        if value is not None:
            return str(value)
    return default


def _render_plan_text(data: dict[str, Any]) -> None:
    actions = data.get("actions", [])
    table = Table("Action", "Kind", "Target")
    for action in actions:
        if not isinstance(action, dict):
            continue
        target = action.get("target")
        if isinstance(target, dict):
            target_text = "/".join(
                str(value)
                for key in ("chart", "profile", "environment", "namespace", "release")
                if (value := target.get(key))
            )
        else:
            target_text = _action_value(action, "chart", "target", default="-")
        action_id = _action_value(action, "id", "actionId", "action_id")
        table.add_row(
            action_id,
            _action_value(action, "kind", "actionKind", "action_kind"),
            target_text or "-",
        )
    console.print(table)


def plan(
    chart: Annotated[str, typer.Argument(help="Chart name.")],
    workflow: WorkflowOption,
    profile: ProfileOption,
    fmt: FormatOption = "text",
    root: RootOption = Path("."),
) -> None:
    """Compile one chart workflow into an ordered lifecycle action plan."""

    data = _compile(root, chart, workflow, profile).to_dict()
    if fmt == "json":
        _json(data)
    elif fmt == "yaml":
        _yaml(data)
    else:
        _render_plan_text(data)


def _doctor(root: Path) -> Any:
    return doctor_lifecycle(root, charts_dir=Settings().charts_dir)


def _status_service(root: Path) -> LifecycleStatusService:
    state_root = root.resolve() / ".chart-manager" / "state"
    return LifecycleStatusService(LocalEvidenceRepository(state_root))


@dataclass
class _LiveObserverBundle:
    observers: tuple[Any, ...]
    warnings: list[str]


class _SafeLiveObserver:
    """Degrade one unavailable live query to unknown for the current run."""

    def __init__(self, observer: Any, warnings: list[str]) -> None:
        self._observer = observer
        self._warnings = warnings
        self._failed = False

    def observe(self, action: Any) -> Any:
        if self._failed:
            return None
        try:
            return self._observer.observe(action)
        except ChartManagerError as exc:
            self._failed = True
            self._warnings.append(f"live observation unavailable: {exc}")
            return None


def _container(settings: Settings) -> Container:
    """Build live-query adapters at a test-replaceable composition seam."""

    return Container(settings)


def _build_live_observers(
    *,
    cluster_name: str,
    kube_context: str | None,
) -> _LiveObserverBundle:
    """Build read-only observers bound to one explicit cluster identity."""

    context = kube_context or f"kind-{cluster_name}"
    container = _container(Settings(kube_context=context))
    identity = ClusterIdentity(name=cluster_name, context=context)
    run_id = f"live-{uuid4()}"
    warnings: list[str] = []
    observers = (
        _SafeLiveObserver(
            HelmReleaseStatusObserver(
                cast(HelmReleaseReader, container.helm(verbose=False)),
                cluster=identity,
                run_id=run_id,
            ),
            warnings,
        ),
        _SafeLiveObserver(
            WorkloadReadinessObserver(
                container.kubectl(),
                cluster=identity,
                run_id=run_id,
            ),
            warnings,
        ),
    )
    return _LiveObserverBundle(observers, warnings)


def _impact_service(root: Path) -> LifecycleImpactService:
    settings = Settings()
    return LifecycleImpactService(
        root,
        charts_dir=settings.charts_dir,
        local_config=settings.local_config,
    )


def _changed_paths(
    changed_files: Path | None,
    changed_file: list[str],
) -> list[str]:
    changes: list[str] = []
    if changed_files is not None:
        try:
            contents = changed_files.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise typer.BadParameter(
                f"cannot read changed-files input {changed_files}: {exc}",
                param_hint="--changed-files",
            ) from exc
        changes.extend(line.strip() for line in contents.splitlines() if line.strip())
    changes.extend(path.strip() for path in changed_file if path.strip())
    if not changes:
        raise typer.BadParameter(
            "provide at least one changed path via --changed-files or --changed-file",
            param_hint="--changed-files / --changed-file",
        )
    return changes


def _render_impact_text(result: Any) -> None:
    typer.echo("Validation:")
    if not result.validation:
        typer.echo("  none")
    for case in result.validation:
        typer.echo(f"  {case.chart}/{case.environment}")
        for reason in case.reasons:
            typer.echo(
                f"    - {reason.code}: {reason.changed_file.as_posix()} — {reason.detail}"
            )

    typer.echo("Cluster tests:")
    if not result.cluster_tests:
        typer.echo("  none")
    for case in result.cluster_tests:
        typer.echo(f"  {case.chart}/{case.profile}")
        for reason in case.reasons:
            typer.echo(
                f"    - {reason.code}: {reason.changed_file.as_posix()} — {reason.detail}"
            )

    if result.warnings:
        typer.echo("Warnings:")
        for warning in result.warnings:
            typer.echo(f"  - {warning}")
    if result.spec_errors:
        typer.echo("Spec errors:")
        for error in result.spec_errors:
            typer.echo(f"  - {error}")


def impact(
    changed_files: Annotated[
        Path | None,
        typer.Option(
            "--changed-files",
            help="Path to a newline-delimited changed-file list.",
        ),
    ] = None,
    changed_file: Annotated[
        list[str],
        typer.Option(
            "--changed-file",
            help="Changed repository-relative path (repeatable).",
        ),
    ] = [],
    fmt: FormatOption = "text",
    root: RootOption = Path("."),
) -> None:
    """Explain validation and cluster-test work selected by changed paths."""

    result = _impact_service(root).analyze(
        _changed_paths(changed_files, changed_file)
    )
    data = result.to_dict()
    if fmt == "json":
        _json(data)
    elif fmt == "yaml":
        _yaml(data)
    else:
        _render_impact_text(result)
    if result.spec_errors:
        raise typer.Exit(1)


def doctor(
    root: RootOption = Path("."),
    fmt: FormatOption = "text",
) -> None:
    """Check lifecycle configuration across the repository."""

    report = _doctor(root)
    if fmt == "json":
        _json(report.to_dict())
    elif fmt == "yaml":
        _yaml(report.to_dict())
    else:
        diagnostics = report.diagnostics
        if not diagnostics:
            console.print("[green]Lifecycle configuration is valid.[/green]")
        else:
            table = Table("Severity", "Chart", "Message")
            for diagnostic in diagnostics:
                table.add_row(
                    str(getattr(diagnostic, "severity", "error")),
                    str(getattr(diagnostic, "chart", "-") or "-"),
                    str(getattr(diagnostic, "message", diagnostic)),
                )
            console.print(table)
    if not report.ok:
        raise typer.Exit(1)


def status(
    chart: Annotated[str, typer.Argument(help="Chart name.")],
    workflow: WorkflowOption = "validation",
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="Profile to inspect. Defaults to dev for validation and minimal for cluster-test.",
        ),
    ] = None,
    live: Annotated[
        bool,
        typer.Option(
            "--live/--no-live",
            help="Query current Helm and Kubernetes state for cluster-test actions.",
        ),
    ] = False,
    cluster_name: Annotated[
        str,
        typer.Option("--cluster-name", help="Cluster identity recorded on live observations."),
    ] = DEFAULT_CLUSTER_NAME,
    kube_context: Annotated[
        str | None,
        typer.Option(
            "--context",
            help="Kubernetes context for live queries. Defaults to kind-<cluster-name>.",
        ),
    ] = None,
    fmt: FormatOption = "text",
    root: RootOption = Path("."),
) -> None:
    """Project current lifecycle actions against cached and optional live evidence."""

    if live and workflow != "cluster-test":
        raise typer.BadParameter(
            "--live is only supported for the cluster-test workflow",
            param_hint="--live",
        )
    effective_profile = profile or ("dev" if workflow == "validation" else "minimal")
    compiled = _compile(root, chart, workflow, effective_profile)
    bundle = _LiveObserverBundle((), [])
    if live:
        try:
            bundle = _build_live_observers(
                cluster_name=cluster_name,
                kube_context=kube_context,
            )
        except ChartManagerError as exc:
            bundle.warnings.append(f"live observation unavailable: {exc}")
    result = _status_service(root).project(compiled, observers=bundle.observers)
    for warning in bundle.warnings:
        typer.echo(f"warning: {warning}", err=True)
    data = result.to_dict()
    if fmt == "json":
        _json(data)
        return
    if fmt == "yaml":
        _yaml(data)
        return

    table = Table("Action", "Kind", "Freshness", "Verdict", "Reason")
    for action in data.get("actions", []):
        table.add_row(
            _action_value(action, "actionId", "action_id"),
            _action_value(action, "kind"),
            _action_value(action, "freshness", default="unknown"),
            _action_value(action, "verdict", default="-") or "-",
            _action_value(action, "reason", default="-") or "-",
        )
    console.print(table)
    for diagnostic in data.get("diagnostics", []):
        if isinstance(diagnostic, dict):
            console.print(
                f"[yellow]warning:[/yellow] {diagnostic.get('path', '-')}: "
                f"{diagnostic.get('message', '')}"
            )
