"""Load and compile the fixed repository ``ChartWorkspace`` resource."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePath

from pydantic import ValidationError

from chart_manager.api.v1alpha1.chart_workspace import ChartWorkspace, ChartWorkspaceSpec
from chart_manager.plumbing.errors import SpecError, WorkspaceNotFoundError, YamlError
from chart_manager.plumbing.yaml_files import load_yaml_file

WORKSPACE_FILE = Path(".chart-manager/workspace.yaml")
SCHEMA_LOCK_FILE = Path(".chart-manager/schemas.lock.yaml")


@dataclass(frozen=True)
class RepositoryWorkspace:
    """One compiled, immutable interpretation of a repository checkout."""

    root: Path
    name: str
    spec: ChartWorkspaceSpec

    def with_charts_dir(self, path: Path) -> RepositoryWorkspace:
        """Return this workspace with ``chartsDir`` re-pointed at ``path``."""
        try:
            spec = ChartWorkspaceSpec.model_validate(
                {**self.spec.model_dump(by_alias=True), "chartsDir": path}
            )
        except ValidationError as exc:
            message = str(exc.errors()[0]["msg"]).removeprefix("Value error, ")
            raise SpecError(f"invalid chart directory {path}: {message}") from exc
        _check_layout(self.root, spec)
        return RepositoryWorkspace(root=self.root, name=self.name, spec=spec)

    @property
    def marker(self) -> Path:
        return self.root / WORKSPACE_FILE

    @property
    def charts_root(self) -> Path:
        """Absolute directory containing managed chart directories."""
        return self.root / self.spec.charts_dir

    @property
    def local_cluster_path(self) -> Path:
        return self.root / self.spec.local_cluster

    @property
    def render_root(self) -> Path:
        return self.root / self.spec.render_dir

    @property
    def policies_root(self) -> Path:
        return self.root / self.spec.policies_dir

    def chart_path(self, name: str) -> Path:
        """Absolute path for one managed chart name."""
        return self.charts_root / name

    def repo_chart_path(self, name: str, *children: str) -> Path:
        """Repository-relative path beneath one managed chart."""
        return self.spec.charts_dir / name / Path(*children)

    def chart_name_from_repo_path(self, path: PurePath | str) -> str | None:
        """Return the managed chart name owning a repository-relative path."""
        parts = PurePath(path).parts
        prefix = self.spec.charts_dir.parts
        if len(parts) <= len(prefix) or parts[: len(prefix)] != prefix:
            return None
        return parts[len(prefix)]


def pattern_matches(pattern: str, path: PurePath | str) -> bool:
    """Case-sensitive POSIX matching with recursive ``**`` semantics."""
    path_parts = PurePath(path).as_posix().split("/")
    pattern_parts = pattern.split("/")
    has_glob = any("*" in part for part in pattern_parts)
    if not has_glob:
        return path_parts[: len(pattern_parts)] == pattern_parts

    @cache
    def match(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        segment = pattern_parts[pattern_index]
        if segment == "**":
            return match(pattern_index + 1, path_index) or (
                path_index < len(path_parts) and match(pattern_index, path_index + 1)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], segment)
            and match(pattern_index + 1, path_index + 1)
        )

    return match(0, 0)


def discover_workspace_root(start: Path) -> Path | None:
    """Return the nearest ancestor containing the fixed workspace marker."""
    candidate = start.resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for directory in (candidate, *candidate.parents):
        if (directory / WORKSPACE_FILE).is_file():
            return directory
    return None


def resolve_repository_root(*, configured: Path | None, start: Path | None = None) -> Path:
    """Return the operator override, else the nearest ancestor holding the marker."""
    if configured is not None:
        return configured.resolve()
    origin = (start or Path.cwd()).resolve()
    discovered = discover_workspace_root(origin)
    if discovered is None:
        raise WorkspaceNotFoundError(
            f"no {WORKSPACE_FILE.as_posix()} in {origin} or any parent; run from a "
            "chart repository checkout or set CHART_MANAGER_ROOT"
        )
    return discovered


def load_repository_workspace(root: Path) -> RepositoryWorkspace:
    """Load and compile the fixed workspace resource under ``root``."""
    resolved_root = root.resolve()
    marker = resolved_root / WORKSPACE_FILE
    if not marker.is_file():
        raise WorkspaceNotFoundError(f"{resolved_root} has no {WORKSPACE_FILE.as_posix()}")
    try:
        document = load_yaml_file(marker)
        resource = ChartWorkspace.model_validate(document)
    except (YamlError, ValidationError, ValueError) as exc:
        raise SpecError(f"invalid ChartWorkspace {marker}: {exc}") from exc
    _check_layout(resolved_root, resource.spec)
    return RepositoryWorkspace(root=resolved_root, name=resource.metadata.name, spec=resource.spec)


def _check_layout(root: Path, spec: ChartWorkspaceSpec) -> None:
    """Reject layout paths that resolve outside ``root`` and a symlinked ``renderDir``."""
    for field, relative in (
        ("spec.chartsDir", spec.charts_dir),
        ("spec.localCluster", spec.local_cluster),
        ("spec.renderDir", spec.render_dir),
        ("spec.policiesDir", spec.policies_dir),
    ):
        resolved = (root / relative).resolve()
        if not resolved.is_relative_to(root):
            raise SpecError(f"{field} resolves outside repository root: {relative}")
        if field == "spec.renderDir":
            current = root
            for part in relative.parts:
                current /= part
                if current.is_symlink():
                    raise SpecError(
                        f"spec.renderDir must not contain symlink components: {relative}"
                    )


__all__ = [
    "SCHEMA_LOCK_FILE",
    "WORKSPACE_FILE",
    "RepositoryWorkspace",
    "discover_workspace_root",
    "load_repository_workspace",
    "pattern_matches",
    "resolve_repository_root",
]
