"""Read-only catalog of Helm charts and authored lifecycle capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.api.lifecycle.v1alpha1 import ChartLifecycle
from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.services.chart_config import (
    LIFECYCLE_FILENAME,
    CapabilityStatus,
    cluster_test_status,
    load_optional_chart_lifecycle,
    validate_chart_lifecycle_identity,
    validation_status,
)
from chart_manager.services.domain.charts import ChartDependency, ChartRepository
from chart_manager.settings import DEFAULT_CHARTS_DIR


@dataclass(frozen=True)
class ChartCatalogEntry:
    """One chart's Helm metadata and best-effort lifecycle status."""

    name: str
    version: str = "?"
    chart_type: str = "?"
    dependencies: tuple[str, ...] = ()
    lifecycle_status: str = "absent"
    validation: CapabilityStatus = CapabilityStatus.ABSENT
    cluster_test: CapabilityStatus = CapabilityStatus.ABSENT
    profiles: tuple[str, ...] = ()
    error: str | None = None


class ChartCatalogService:
    """Inspect Helm charts and their optional lifecycle intent."""

    def __init__(self, root: Path, *, charts_dir: Path = DEFAULT_CHARTS_DIR) -> None:
        """Build the Helm repository from the chart repo root."""
        self.repository = ChartRepository(root, charts_dir=charts_dir)

    def list_entries(self) -> list[ChartCatalogEntry]:
        """Return every chart, retaining malformed metadata/intent diagnostics."""
        return [self._entry(name) for name in self.repository.list_names()]

    def get_lifecycle(self, name: str) -> ChartLifecycle:
        """Strictly return one chart's composed lifecycle intent."""
        chart = self.repository.get(name)
        lifecycle = load_optional_chart_lifecycle(chart.path / LIFECYCLE_FILENAME)
        if lifecycle is None:
            raise SpecError(
                f"chart '{name}' has no lifecycle configuration in {LIFECYCLE_FILENAME}"
            )
        validate_chart_lifecycle_identity(
            lifecycle,
            chart_name=chart.name,
            chart_directory=chart.path,
        )
        return lifecycle

    def _entry(self, name: str) -> ChartCatalogEntry:
        try:
            chart = self.repository.get(name)
            lifecycle = load_optional_chart_lifecycle(chart.path / LIFECYCLE_FILENAME)
            if lifecycle is not None:
                validate_chart_lifecycle_identity(
                    lifecycle,
                    chart_name=chart.name,
                    chart_directory=chart.path,
                )
        except ChartManagerError as exc:
            return ChartCatalogEntry(
                name=name,
                lifecycle_status="invalid",
                error=str(exc),
            )

        if lifecycle is None:
            return ChartCatalogEntry(
                name=chart.name,
                version=chart.metadata.version or "",
                chart_type=chart.metadata.chart_type,
                dependencies=_dependencies(chart.metadata.dependencies),
            )

        manifest_status = validation_status(lifecycle)
        cluster_status = cluster_test_status(lifecycle)
        profiles = (
            tuple(sorted(lifecycle.spec.cluster_test.profiles))
            if cluster_status is CapabilityStatus.ENABLED
            and lifecycle.spec.cluster_test is not None
            else ()
        )
        return ChartCatalogEntry(
            name=chart.name,
            version=chart.metadata.version or "",
            chart_type=chart.metadata.chart_type,
            dependencies=_dependencies(chart.metadata.dependencies),
            lifecycle_status="enabled" if lifecycle.spec.enabled else "disabled",
            validation=manifest_status,
            cluster_test=cluster_status,
            profiles=profiles,
        )


def _dependencies(dependencies: tuple[ChartDependency, ...]) -> tuple[str, ...]:
    """Render dependency names and versions without leaking models to the CLI."""
    rendered: list[str] = []
    for dependency in dependencies:
        rendered.append(f"{dependency.name} {dependency.version or '?'}")
    return tuple(rendered)
