"""Typed Helm-chart metadata and repository-backed chart loading."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from chart_manager.plumbing.errors import ChartNotFoundError, SpecError
from chart_manager.plumbing.yaml_files import load_yaml_file
from chart_manager.services.domain.spec import TestSpec, load_test_spec


@dataclass(frozen=True)
class ChartDependency:
    """One dependency declared in a Helm ``Chart.yaml``."""

    name: str
    version: str | None = None
    repository: str | None = None
    alias: str | None = None


@dataclass(frozen=True)
class ChartMetadata:
    """The Helm metadata used by chart-manager."""

    name: str
    version: str | None
    chart_type: str
    dependencies: tuple[ChartDependency, ...]


@dataclass(frozen=True)
class HelmChart:
    """A Helm chart, independent of chart-manager's test specification."""

    name: str
    path: Path
    metadata: ChartMetadata


@dataclass(frozen=True)
class ManagedChart:
    """A Helm chart that participates in chart-manager test workflows."""

    chart: HelmChart
    spec: TestSpec

    @property
    def name(self) -> str:
        """Return the underlying Helm chart name."""
        return self.chart.name

    @property
    def path(self) -> Path:
        """Return the underlying Helm chart directory."""
        return self.chart.path

    @property
    def metadata(self) -> ChartMetadata:
        """Return the underlying Helm metadata."""
        return self.chart.metadata


def load_chart_metadata(path: Path) -> ChartMetadata:
    """Strictly load the chart-manager subset of a Helm ``Chart.yaml``.

    The loader raises ``SpecError`` for malformed YAML or fields of the wrong
    shape. Callers performing best-effort repository scans may catch that
    error, while callers loading an explicitly requested chart surface it.
    """
    try:
        data = load_yaml_file(path)
    except yaml.YAMLError as exc:
        raise SpecError(f"failed to parse {path}: {exc}") from exc

    name = _required_string(data, "name", path)
    version = _optional_string(data, "version", path)
    chart_type = _optional_string(data, "type", path) or "application"

    dependencies_raw = data.get("dependencies", [])
    if dependencies_raw is None:
        dependencies_raw = []
    if not isinstance(dependencies_raw, list):
        raise SpecError(f"{path} field 'dependencies' must be a list")

    dependencies: list[ChartDependency] = []
    for index, dependency_raw in enumerate(dependencies_raw):
        field = f"dependencies[{index}]"
        if not isinstance(dependency_raw, dict):
            raise SpecError(f"{path} field '{field}' must be a mapping")
        dependency: dict[str, Any] = dependency_raw
        dependencies.append(
            ChartDependency(
                name=_required_string(dependency, "name", path, parent=field),
                version=_optional_string(dependency, "version", path, parent=field),
                repository=_optional_string(
                    dependency, "repository", path, parent=field
                ),
                alias=_optional_string(dependency, "alias", path, parent=field),
            )
        )

    return ChartMetadata(
        name=name,
        version=version,
        chart_type=chart_type,
        dependencies=tuple(dependencies),
    )


class ChartRepository:
    """Look up Helm and chart-manager-managed charts under ``<root>/charts``."""

    def __init__(self, root: Path) -> None:
        """Anchor the repository at the resolved repo root."""
        self.root = root.resolve()
        self.charts_dir = self.root / "charts"

    def list_names(self) -> list[str]:
        """Return sorted names of chart directories containing a Chart.yaml."""
        if not self.charts_dir.exists():
            return []
        names = [
            path.name
            for path in self.charts_dir.iterdir()
            if path.is_dir() and (path / "Chart.yaml").exists()
        ]
        return sorted(names)

    def get(self, name: str) -> HelmChart:
        """Load Helm metadata for a chart; no test specification is required."""
        path = self.charts_dir / name
        chart_yaml_path = path / "Chart.yaml"
        if not chart_yaml_path.exists():
            raise ChartNotFoundError(f"chart not found: {name}")
        metadata = load_chart_metadata(chart_yaml_path)
        if metadata.name != name:
            raise SpecError(
                f"{chart_yaml_path} name '{metadata.name}' does not match directory '{name}'"
            )
        return HelmChart(name=name, path=path, metadata=metadata)

    def get_managed(self, name: str) -> ManagedChart:
        """Load a chart and require its chart-manager ``test-spec.yaml``."""
        chart = self.get(name)
        return ManagedChart(
            chart=chart,
            spec=load_test_spec(chart.path / "test-spec.yaml"),
        )

    def value_paths(self, chart: ManagedChart, profile: str) -> list[Path]:
        """Resolve a profile's values files to absolute paths, all must exist."""
        profile_spec = chart.spec.profile(profile)
        paths = [chart.path / value for value in profile_spec.values]
        missing = [path for path in paths if not path.exists()]
        if missing:
            rendered = ", ".join(str(path) for path in missing)
            raise SpecError(f"missing values file(s) for {chart.name}:{profile}: {rendered}")
        return paths


def _required_string(
    data: dict[str, Any],
    key: str,
    path: Path,
    *,
    parent: str | None = None,
) -> str:
    value = data.get(key)
    label = f"{parent}.{key}" if parent else key
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{path} field '{label}' must be a non-empty string")
    return value


def _optional_string(
    data: dict[str, Any],
    key: str,
    path: Path,
    *,
    parent: str | None = None,
) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    label = f"{parent}.{key}" if parent else key
    if not isinstance(value, str):
        raise SpecError(f"{path} field '{label}' must be a string")
    return value
