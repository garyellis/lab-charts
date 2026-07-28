"""Catalog charts with enabled manifest-validation capabilities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.services.chart_config import (
    LIFECYCLE_FILENAME,
    CapabilityStatus,
    load_optional_chart_lifecycle,
    require_validation,
    validate_chart_lifecycle_identity,
    validation_status,
)
from chart_manager.services.domain.charts import ChartRepository
from chart_manager.services.manifest_validation.models import ManifestValidationTarget
from chart_manager.settings import DEFAULT_CHARTS_DIR


def discover_chart_lifecycle(chart_path: Path) -> Path | None:
    """Return lifecycle intent when it is a regular file."""
    candidate = chart_path / LIFECYCLE_FILENAME
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


def load_manifest_validation_target(
    root: Path,
    name: str,
    *,
    charts_dir: Path = DEFAULT_CHARTS_DIR,
) -> ManifestValidationTarget:
    """Strictly load one explicitly requested manifest-validation target."""
    chart = ChartRepository(root, charts_dir=charts_dir).get(name)
    spec_path = chart.path / LIFECYCLE_FILENAME
    lifecycle = load_optional_chart_lifecycle(spec_path)
    if lifecycle is not None:
        validate_chart_lifecycle_identity(
            lifecycle,
            chart_name=chart.name,
            chart_directory=chart.path,
        )
    return ManifestValidationTarget(
        chart=chart,
        spec=require_validation(lifecycle, chart_name=chart.name),
        spec_path=spec_path,
    )


def load_chart_specs(
    root: Path,
    charts: Iterable[str],
    *,
    charts_dir: Path = DEFAULT_CHARTS_DIR,
) -> list[CatalogEntry]:
    """Compose Helm metadata and validation specs without aborting a full scan."""
    repository = ChartRepository(root, charts_dir=charts_dir)
    entries: list[CatalogEntry] = []
    for name in charts:
        try:
            chart = repository.get(name)
        except ChartManagerError as exc:
            entries.append(CatalogEntry(chart=name, error=str(exc)))
            continue
        spec_path = chart.path / LIFECYCLE_FILENAME
        try:
            lifecycle = load_optional_chart_lifecycle(spec_path)
            if lifecycle is not None:
                validate_chart_lifecycle_identity(
                    lifecycle,
                    chart_name=chart.name,
                    chart_directory=chart.path,
                )
        except SpecError as exc:
            entries.append(CatalogEntry(chart=name, error=str(exc)))
            continue
        status = validation_status(lifecycle)
        if status is not CapabilityStatus.ENABLED:
            entries.append(
                CatalogEntry(
                    chart=name,
                    config_missing=lifecycle is None,
                    capability_status=status,
                )
            )
            continue
        # The status check proves the root and capability are enabled.
        spec = require_validation(lifecycle, chart_name=chart.name)
        entries.append(
            CatalogEntry(
                chart=name,
                target=ManifestValidationTarget(chart=chart, spec=spec, spec_path=spec_path),
                capability_status=status,
            )
        )
    return entries


def build_catalog(
    root: Path, *, charts_dir: Path = DEFAULT_CHARTS_DIR
) -> ValidationCatalog:
    """Discover all repository charts, intentionally retaining per-chart errors."""
    root = root.resolve()
    repository = ChartRepository(root, charts_dir=charts_dir)
    entries = load_chart_specs(root, repository.list_names(), charts_dir=charts_dir)
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
                f"chart {name} has no {LIFECYCLE_FILENAME} — skipping manifest validation"
                for name in config_missing
            ),
            *(
                f"chart {name} has no validation configuration in "
                f"{LIFECYCLE_FILENAME} — skipping"
                for name in absent
            ),
            *(
                f"manifest validation is disabled for chart {name} — skipping"
                for name in disabled
            ),
        ),
        chart_count_unvalidated=inactive_count,
    )
