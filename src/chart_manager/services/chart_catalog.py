"""Read-only catalog of Helm charts and chart-manager capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.services.chart_config import (
    CONFIG_FILENAME,
    CapabilityStatus,
    ChartManagerConfig,
    cluster_tests_status,
    load_optional_chart_manager_config,
    manifest_validation_status,
)
from chart_manager.services.domain.charts import ChartDependency, ChartRepository


@dataclass(frozen=True)
class ChartCatalogEntry:
    """One chart's Helm metadata and best-effort configuration status."""

    name: str
    version: str = "?"
    chart_type: str = "?"
    dependencies: tuple[str, ...] = ()
    config_status: str = "absent"
    manifest_validation: CapabilityStatus = CapabilityStatus.ABSENT
    cluster_tests: CapabilityStatus = CapabilityStatus.ABSENT
    profiles: tuple[str, ...] = ()
    error: str | None = None


class ChartCatalogService:
    """Inspect Helm charts and their optional chart-manager configuration."""

    def __init__(self, root: Path) -> None:
        """Build the Helm repository from the chart repo root."""
        self.repository = ChartRepository(root)

    def list_entries(self) -> list[ChartCatalogEntry]:
        """Return every chart, retaining malformed metadata/config diagnostics."""
        return [self._entry(name) for name in self.repository.list_names()]

    def get_config(self, name: str) -> ChartManagerConfig:
        """Strictly return one chart's authored configuration."""
        chart = self.repository.get(name)
        config = load_optional_chart_manager_config(chart.path / CONFIG_FILENAME)
        if config is None:
            raise SpecError(
                f"chart '{name}' has no chart-manager configuration in {CONFIG_FILENAME}"
            )
        return config

    def _entry(self, name: str) -> ChartCatalogEntry:
        try:
            chart = self.repository.get(name)
            config = load_optional_chart_manager_config(chart.path / CONFIG_FILENAME)
        except ChartManagerError as exc:
            return ChartCatalogEntry(
                name=name,
                config_status="invalid",
                error=str(exc),
            )

        if config is None:
            return ChartCatalogEntry(
                name=chart.name,
                version=chart.metadata.version or "",
                chart_type=chart.metadata.chart_type,
                dependencies=_dependencies(chart.metadata.dependencies),
            )

        manifest_status = manifest_validation_status(config)
        cluster_status = cluster_tests_status(config)
        profiles = (
            tuple(sorted(config.cluster_tests.profiles))
            if cluster_status is CapabilityStatus.ENABLED
            and config.cluster_tests is not None
            else ()
        )
        return ChartCatalogEntry(
            name=chart.name,
            version=chart.metadata.version or "",
            chart_type=chart.metadata.chart_type,
            dependencies=_dependencies(chart.metadata.dependencies),
            config_status="enabled" if config.enabled else "disabled",
            manifest_validation=manifest_status,
            cluster_tests=cluster_status,
            profiles=profiles,
        )


def _dependencies(dependencies: tuple[ChartDependency, ...]) -> tuple[str, ...]:
    """Render dependency names and versions without leaking models to the CLI."""
    rendered: list[str] = []
    for dependency in dependencies:
        rendered.append(f"{dependency.name} {dependency.version or '?'}")
    return tuple(rendered)
