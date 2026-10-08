"""`chart list` and `chart show`: read each chart and its authored lifecycle capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.api.v1alpha1.chart_lifecycle import ChartLifecycle
from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.shared.charts.chart import ChartDependency, chart_names, load_chart
from chart_manager.shared.charts.lifecycle import (
    LIFECYCLE_FILENAME,
    CapabilityStatus,
    chart_test_status,
    validation_status,
)
from chart_manager.shared.workspace import RepositoryWorkspace


@dataclass(frozen=True)
class ChartCatalogEntry:
    """One chart's Helm metadata and best-effort lifecycle status."""

    name: str
    version: str = "?"
    chart_type: str = "?"
    dependencies: tuple[str, ...] = ()
    lifecycle_status: str = "absent"
    validation: CapabilityStatus = CapabilityStatus.ABSENT
    chart_test: CapabilityStatus = CapabilityStatus.ABSENT
    profiles: tuple[str, ...] = ()
    error: str | None = None


def list_charts(workspace: RepositoryWorkspace) -> list[ChartCatalogEntry]:
    """Return every chart, retaining malformed metadata/intent diagnostics."""
    return [_entry(workspace.chart_path(name)) for name in chart_names(workspace.charts_root)]


def show_chart(workspace: RepositoryWorkspace, name: str) -> ChartLifecycle:
    """Strictly return one chart's composed lifecycle intent."""
    lifecycle = load_chart(workspace.charts_root / name).lifecycle
    if lifecycle is None:
        raise SpecError(f"chart '{name}' has no lifecycle configuration in {LIFECYCLE_FILENAME}")
    return lifecycle


def _entry(path: Path) -> ChartCatalogEntry:
    try:
        chart = load_chart(path)
    except ChartManagerError as exc:
        return ChartCatalogEntry(name=path.name, lifecycle_status="invalid", error=str(exc))

    lifecycle = chart.lifecycle
    if lifecycle is None:
        return ChartCatalogEntry(
            name=chart.name,
            version=chart.metadata.version or "",
            chart_type=chart.metadata.chart_type,
            dependencies=_dependencies(chart.metadata.dependencies),
        )

    manifest_status = validation_status(lifecycle)
    cluster_status = chart_test_status(lifecycle)
    profiles = (
        tuple(sorted(lifecycle.spec.chart_test.profiles))
        if cluster_status is CapabilityStatus.ENABLED
        and lifecycle.spec.chart_test is not None
        else ()
    )
    return ChartCatalogEntry(
        name=chart.name,
        version=chart.metadata.version or "",
        chart_type=chart.metadata.chart_type,
        dependencies=_dependencies(chart.metadata.dependencies),
        lifecycle_status="enabled" if lifecycle.spec.enabled else "disabled",
        validation=manifest_status,
        chart_test=cluster_status,
        profiles=profiles,
    )


def _dependencies(dependencies: tuple[ChartDependency, ...]) -> tuple[str, ...]:
    """Render dependency names and versions without leaking models to the CLI."""
    rendered: list[str] = []
    for dependency in dependencies:
        rendered.append(f"{dependency.name} {dependency.version or '?'}")
    return tuple(rendered)
