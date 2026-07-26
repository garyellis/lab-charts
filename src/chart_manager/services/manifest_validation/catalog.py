"""Catalog charts with enabled manifest-validation capabilities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.services.chart_config import (
    CONFIG_FILENAME,
    CapabilityStatus,
    load_optional_chart_manager_config,
    manifest_validation_status,
    require_manifest_validation,
)
from chart_manager.services.domain.charts import ChartRepository
from chart_manager.services.manifest_validation.models import ManifestValidationTarget


def discover_chart_manager_config(chart_path: Path) -> Path | None:
    """Return the chart-manager configuration when it is a regular file."""
    candidate = chart_path / CONFIG_FILENAME
    return candidate if candidate.is_file() else None


@dataclass(frozen=True)
class CatalogEntry:
    """Best-effort load outcome for one repository chart."""

    chart: str
    target: ManifestValidationTarget | None = None
    error: str | None = None
    config_missing: bool = False
    capability_status: CapabilityStatus = CapabilityStatus.ABSENT


@dataclass(frozen=True)
class ValidationCatalog:
    """Repository-wide validation targets and discovery diagnostics."""

    targets: tuple[ManifestValidationTarget, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    chart_count_unvalidated: int = 0

    def by_name(self) -> dict[str, ManifestValidationTarget]:
        """Index enabled targets by their authoritative chart name."""
        return {target.name: target for target in self.targets}


def load_manifest_validation_target(root: Path, name: str) -> ManifestValidationTarget:
    """Strictly load one explicitly requested manifest-validation target."""
    chart = ChartRepository(root).get(name)
    spec_path = chart.path / CONFIG_FILENAME
    config = load_optional_chart_manager_config(spec_path)
    return ManifestValidationTarget(
        chart=chart,
        spec=require_manifest_validation(config, chart_name=chart.name),
        spec_path=spec_path,
    )


def load_chart_specs(
    root: Path,
    charts: Iterable[str],
) -> list[CatalogEntry]:
    """Compose Helm metadata and validation specs without aborting a full scan."""
    repository = ChartRepository(root)
    entries: list[CatalogEntry] = []
    for name in charts:
        try:
            chart = repository.get(name)
        except ChartManagerError as exc:
            entries.append(CatalogEntry(chart=name, error=str(exc)))
            continue
        spec_path = chart.path / CONFIG_FILENAME
        try:
            config = load_optional_chart_manager_config(spec_path)
        except SpecError as exc:
            entries.append(CatalogEntry(chart=name, error=str(exc)))
            continue
        status = manifest_validation_status(config)
        if status is not CapabilityStatus.ENABLED:
            entries.append(
                CatalogEntry(
                    chart=name,
                    config_missing=config is None,
                    capability_status=status,
                )
            )
            continue
        # The status check proves the root and capability are enabled.
        spec = require_manifest_validation(config, chart_name=chart.name)
        entries.append(
            CatalogEntry(
                chart=name,
                target=ManifestValidationTarget(chart=chart, spec=spec, spec_path=spec_path),
                capability_status=status,
            )
        )
    return entries


def build_catalog(root: Path) -> ValidationCatalog:
    """Discover all repository charts, intentionally retaining per-chart errors."""
    root = root.resolve()
    repository = ChartRepository(root)
    entries = load_chart_specs(root, repository.list_names())
    targets = tuple(entry.target for entry in entries if entry.target is not None)
    errors = tuple(f"{entry.chart}: {entry.error}" for entry in entries if entry.error is not None)
    config_missing = tuple(entry.chart for entry in entries if entry.config_missing)
    absent = tuple(
        entry.chart
        for entry in entries
        if not entry.config_missing
        and entry.error is None
        and entry.capability_status is CapabilityStatus.ABSENT
    )
    disabled = tuple(
        entry.chart
        for entry in entries
        if entry.error is None and entry.capability_status is CapabilityStatus.DISABLED
    )
    inactive_count = len(config_missing) + len(absent) + len(disabled)
    return ValidationCatalog(
        targets=targets,
        errors=errors,
        warnings=(
            *(
                f"chart {name} has no {CONFIG_FILENAME} — skipping manifest validation"
                for name in config_missing
            ),
            *(
                f"chart {name} has no manifestValidation configuration in "
                f"{CONFIG_FILENAME} — skipping"
                for name in absent
            ),
            *(
                f"manifest validation is disabled for chart {name} — skipping"
                for name in disabled
            ),
        ),
        chart_count_unvalidated=inactive_count,
    )
