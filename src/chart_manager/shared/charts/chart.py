"""Load a chart directory: its Helm metadata and its optional lifecycle."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chart_manager.api.v1alpha1.chart_lifecycle import ChartLifecycle
from chart_manager.plumbing.errors import ChartNotFoundError, SpecError, YamlError
from chart_manager.plumbing.names import dns_label
from chart_manager.plumbing.paths import inside_root
from chart_manager.plumbing.yaml_files import load_yaml_file
from chart_manager.shared.charts.lifecycle import (
    LIFECYCLE_FILENAME,
    load_optional_chart_lifecycle,
    validate_chart_lifecycle_identity,
)
from chart_manager.shared.workspace import RepositoryWorkspace


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
class Chart:
    """A chart directory: its Helm metadata and its optional lifecycle, names agreeing."""

    name: str
    path: Path
    metadata: ChartMetadata
    lifecycle: ChartLifecycle | None


def load_chart(path: Path) -> Chart:
    """Load the chart in ``path``; directory, Chart.yaml and lifecycle names must agree."""
    chart_yaml = path / "Chart.yaml"
    if not chart_yaml.exists():
        raise ChartNotFoundError(f"chart not found: {path}")
    metadata = _load_metadata(chart_yaml)
    if metadata.name != path.name:
        raise SpecError(
            f"{chart_yaml} name '{metadata.name}' does not match directory '{path.name}'"
        )
    lifecycle = load_optional_chart_lifecycle(path / LIFECYCLE_FILENAME)
    if lifecycle is not None:
        validate_chart_lifecycle_identity(lifecycle, chart_name=metadata.name, chart_directory=path)
    return Chart(name=metadata.name, path=path, metadata=metadata, lifecycle=lifecycle)


def _load_metadata(path: Path) -> ChartMetadata:
    """Strictly load the ``Chart.yaml`` at ``path``; ``SpecError`` when it is malformed."""
    try:
        data = load_yaml_file(path)
    except YamlError as exc:
        raise SpecError(f"failed to load {path}: {exc}") from exc

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
                repository=_optional_string(dependency, "repository", path, parent=field),
                alias=_optional_string(dependency, "alias", path, parent=field),
            )
        )

    return ChartMetadata(
        name=name,
        version=version,
        chart_type=chart_type,
        dependencies=tuple(dependencies),
    )


def chart_names(charts_dir: Path) -> list[str]:
    """Return sorted names of the directories in ``charts_dir`` that contain a Chart.yaml."""
    if not charts_dir.exists():
        return []
    names = [
        path.name
        for path in charts_dir.iterdir()
        if path.is_dir() and (path / "Chart.yaml").exists()
    ]
    return sorted(names)


def resolve_chart_target(workspace: RepositoryWorkspace, chart: str) -> Chart:
    """Load a chart named under the workspace's charts directory, or a chart directory.

    A bare name that is not a path under the repository root is looked up in `chartsDir`.
    """
    if not chart or chart != chart.strip():
        raise SpecError("chart must be a non-empty chart name or directory")
    root = workspace.root.resolve()
    candidate = Path(chart)
    path = candidate if candidate.is_absolute() else root / candidate
    if not path.exists() and len(candidate.parts) == 1 and not candidate.is_absolute():
        path = root / workspace.spec.charts_dir / candidate
    return chart_target(root, path)


def chart_target(root: Path, path: Path) -> Chart:
    """Load the chart directory at `path`, which must sit inside `root`."""
    chart = load_chart(inside_root(root, path))
    try:
        dns_label(chart.name, field="Chart.yaml name")
    except ValueError as exc:
        raise SpecError(f"invalid chart target {path}: {exc}") from exc
    return chart


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
