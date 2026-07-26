"""Best-effort discovery of charts carrying validation specifications."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.services.domain.charts import ChartRepository
from chart_manager.services.validate.domain.models import ValidatableChart
from chart_manager.services.validate.domain.spec import load_validate_spec


def discover_validate_spec(chart_path: Path) -> Path | None:
    """Return a chart's ``validate-spec.yaml`` when it is a regular file."""
    candidate = chart_path / "validate-spec.yaml"
    return candidate if candidate.is_file() else None


@dataclass(frozen=True)
class CatalogEntry:
    """Best-effort load outcome for one repository chart."""

    chart: str
    target: ValidatableChart | None = None
    error: str | None = None
    missing: bool = False


@dataclass(frozen=True)
class ValidationCatalog:
    """Repository-wide validation targets and discovery diagnostics."""

    targets: tuple[ValidatableChart, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    chart_count_unvalidated: int = 0

    def by_name(self) -> dict[str, ValidatableChart]:
        """Index non-skipped targets by their authoritative chart name."""
        return {target.name: target for target in self.targets if not target.spec.skip}


def load_validatable_chart(root: Path, name: str) -> ValidatableChart:
    """Strictly load one explicitly requested validation target."""
    chart = ChartRepository(root).get(name)
    spec_path = chart.path / "validate-spec.yaml"
    return ValidatableChart(
        chart=chart,
        spec=load_validate_spec(spec_path),
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
        spec_path = discover_validate_spec(chart.path)
        if spec_path is None:
            entries.append(CatalogEntry(chart=name, missing=True))
            continue
        try:
            spec = load_validate_spec(spec_path)
        except SpecError as exc:
            entries.append(CatalogEntry(chart=name, error=str(exc)))
            continue
        entries.append(
            CatalogEntry(
                chart=name,
                target=ValidatableChart(chart=chart, spec=spec, spec_path=spec_path),
            )
        )
    return entries


def build_catalog(root: Path) -> ValidationCatalog:
    """Discover all repository charts, intentionally retaining per-chart errors."""
    root = root.resolve()
    repository = ChartRepository(root)
    entries = load_chart_specs(root, repository.list_names())
    targets = tuple(entry.target for entry in entries if entry.target is not None)
    errors = tuple(
        f"{entry.chart}: {entry.error}"
        for entry in entries
        if entry.error is not None
    )
    missing = tuple(entry.chart for entry in entries if entry.missing)
    return ValidationCatalog(
        targets=targets,
        errors=errors,
        warnings=tuple(
            f"chart {name} has no validate-spec.yaml — skipping" for name in missing
        ),
        chart_count_unvalidated=len(missing),
    )
