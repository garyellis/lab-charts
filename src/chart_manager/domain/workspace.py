"""Load and compile the fixed repository ``ChartWorkspace`` resource."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePath

from pydantic import ValidationError

from chart_manager.api.v1alpha1.chart_workspace import ChartWorkspace, WorkspaceValidation
from chart_manager.api.v1alpha1.releases import LifecycleRelease, LocalChartRelease
from chart_manager.plumbing.errors import SpecError, WorkspaceNotFoundError, YamlError
from chart_manager.plumbing.yaml_files import load_yaml_file

WORKSPACE_FILE = Path(".chart-manager/workspace.yaml")
SCHEMA_LOCK_FILE = Path(".chart-manager/schemas.lock.yaml")


@dataclass(frozen=True)
class RepositoryWorkspace:
    """One compiled, immutable interpretation of a repository checkout."""

    root: Path
    name: str
    charts_dir: Path
    local_cluster: Path
    render_dir: Path
    policies_dir: Path
    validation: WorkspaceValidation | None = None
    validation_fanout: tuple[str, ...] = ()
    cluster_test_fanout: tuple[str, ...] = ()
    shared_prerequisites: tuple[str, ...] = ()

    @property
    def marker(self) -> Path:
        return self.root / WORKSPACE_FILE

    @property
    def charts_root(self) -> Path:
        """Absolute directory containing managed chart directories."""
        return self.root if self.charts_dir == Path(".") else self.root / self.charts_dir

    @property
    def local_cluster_path(self) -> Path:
        return self.root / self.local_cluster

    @property
    def render_root(self) -> Path:
        return self.root / self.render_dir

    @property
    def policies_root(self) -> Path:
        return self.root / self.policies_dir

    def chart_path(self, name: str) -> Path:
        """Absolute path for one managed chart name."""
        return self.charts_root / name

    def repo_chart_path(self, name: str, *children: str) -> Path:
        """Repository-relative path beneath one managed chart."""
        base = Path(name) if self.charts_dir == Path(".") else self.charts_dir / name
        return base / Path(*children)

    def chart_name_from_repo_path(self, path: PurePath | str) -> str | None:
        """Return the managed chart name owning a repository-relative path."""
        parts = PurePath(path).parts
        prefix = () if self.charts_dir == Path(".") else self.charts_dir.parts
        if len(parts) <= len(prefix) or parts[: len(prefix)] != prefix:
            return None
        return parts[len(prefix)]

    def matches_validation_fanout(self, path: PurePath | str) -> bool:
        return any(_pattern_matches(pattern, path) for pattern in self.validation_patterns())

    def matches_cluster_test_fanout(self, path: PurePath | str) -> bool:
        return any(_pattern_matches(pattern, path) for pattern in self.cluster_test_patterns())

    def matching_validation_patterns(self, path: PurePath | str) -> tuple[str, ...]:
        return tuple(
            pattern for pattern in self.validation_patterns() if _pattern_matches(pattern, path)
        )

    def matching_cluster_test_patterns(self, path: PurePath | str) -> tuple[str, ...]:
        return tuple(
            pattern
            for pattern in self.cluster_test_patterns()
            if _pattern_matches(pattern, path)
        )

    def validation_patterns(self) -> tuple[str, ...]:
        implicit = (
            _path_pattern(self.policies_dir),
            SCHEMA_LOCK_FILE.as_posix(),
            WORKSPACE_FILE.as_posix(),
        )
        return tuple(sorted({*self.validation_fanout, *implicit}))

    def cluster_test_patterns(self) -> tuple[str, ...]:
        implicit = [self.local_cluster.as_posix(), WORKSPACE_FILE.as_posix()]
        implicit.extend(
            _path_pattern(self.repo_chart_path(name)) for name in self.shared_prerequisites
        )
        # LocalCluster dependency discovery is intentionally lazy. A malformed
        # cluster resource cannot break chart listing or validation.
        if self.local_cluster_path.is_file():
            try:
                from chart_manager.domain.local_resources import load_local_cluster

                cluster = load_local_cluster(self.local_cluster_path)
            except SpecError:
                pass
            else:
                implicit.append(cluster.spec.cluster.config.as_posix())
                implicit.extend(
                    _path_pattern(release.chart)
                    for release in cluster.spec.bootstrap.releases
                    if isinstance(release, (LifecycleRelease, LocalChartRelease))
                )
        return tuple(sorted({*self.cluster_test_fanout, *implicit}))


def _path_pattern(path: Path) -> str:
    return path.as_posix()


def _pattern_matches(pattern: str, path: PurePath | str) -> bool:
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


def resolve_repository_root(
    *,
    configured: Path | None,
    configured_by: str | None = None,
    start: Path | None = None,
) -> Path:
    """Return the workspace root: the operator override, else the nearest marker.

    An explicit root is taken as given -- no walk-up -- so pointing
    `CHART_MANAGER_ROOT` at a subdirectory is an error rather than a silent
    switch to whichever ancestor happens to hold a marker. `configured_by`
    describes where an explicit root came from, path included
    (`CHART_MANAGER_ROOT=/srv/charts`), so the error names the setting to
    fix; `None` means a caller passed the path and the path alone is named.
    """
    if configured is not None:
        resolved = configured.resolve()
        if not (resolved / WORKSPACE_FILE).is_file():
            source = configured_by or str(resolved)
            raise WorkspaceNotFoundError(f"{source} has no {WORKSPACE_FILE.as_posix()}")
        return resolved
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
    spec = resource.spec
    for field, relative in (
        ("spec.chartsDir", spec.charts_dir),
        ("spec.localCluster", spec.local_cluster),
        ("spec.renderDir", spec.render_dir),
        ("spec.policiesDir", spec.policies_dir),
    ):
        resolved = (resolved_root / relative).resolve()
        if not resolved.is_relative_to(resolved_root):
            raise SpecError(f"{field} resolves outside repository root: {relative}")
        if field == "spec.renderDir":
            current = resolved_root
            for part in relative.parts:
                current /= part
                if current.is_symlink():
                    raise SpecError(
                        f"spec.renderDir must not contain symlink components: {relative}"
                    )
    return RepositoryWorkspace(
        root=resolved_root,
        name=resource.metadata.name,
        charts_dir=spec.charts_dir,
        local_cluster=spec.local_cluster,
        render_dir=spec.render_dir,
        policies_dir=spec.policies_dir,
        validation=spec.validation,
        validation_fanout=tuple(spec.fanout.validation),
        cluster_test_fanout=tuple(spec.fanout.cluster_test),
        shared_prerequisites=tuple(spec.cluster_test.shared_prerequisites),
    )


__all__ = [
    "SCHEMA_LOCK_FILE",
    "WORKSPACE_FILE",
    "RepositoryWorkspace",
    "discover_workspace_root",
    "load_repository_workspace",
    "resolve_repository_root",
]
