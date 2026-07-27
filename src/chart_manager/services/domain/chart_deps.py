"""Conservative freshness policy for materialized Helm chart dependencies.

The functions in this module inspect Helm's ``Chart.yaml``, ``Chart.lock``,
and ``charts/`` state.  They never invoke Helm and never raise: uncertainty
means "stale", because a redundant dependency update is safer than rendering
with the wrong dependency.
"""
from __future__ import annotations

import tarfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from chart_manager.plumbing.errors import SpecError
from chart_manager.services.domain.charts import (
    ChartDependency,
    load_chart_metadata,
)

# Dependency archives are untrusted inputs.  Helm packages place Chart.yaml
# near the front of an ordinary tar stream, but we scan the complete bounded
# archive to reject a second/ambiguous root metadata file.
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 2_048
_MAX_ARCHIVE_OFFSET = 128 * 1024 * 1024
_MAX_CHART_YAML_BYTES = 1024 * 1024


@dataclass(frozen=True, order=True)
class _DependencyIdentity:
    name: str
    version: str


def chart_has_dependencies(chart_path: Path) -> bool:
    """Return whether a readable, valid Chart.yaml declares dependencies."""
    try:
        return bool(load_chart_metadata(chart_path / "Chart.yaml").dependencies)
    except SpecError:
        # This is only a pre-template optimization. Helm itself owns the
        # actionable error for a malformed chart passed to `helm template`.
        return False


def deps_are_fresh(chart_path: Path) -> bool:
    """Return whether lock and materialized dependency identities agree.

    A fresh result requires:

    * ``Chart.lock`` and ``charts/`` exist;
    * the lock is no older than ``Chart.yaml``;
    * the source and lock describe compatible dependency names (including
      duplicate dependencies intentionally distinguished by aliases);
    * every unique locked ``(name, version)`` is represented exactly once by
      an expanded chart directory or packaged ``.tgz`` chart; and
    * no additional chart artifact is present.

    Non-chart files and directories are ignored. A chart-looking but
    unreadable/malformed artifact, unsafe archive, duplicate artifact, or
    other ambiguity is stale.
    """
    chart_yaml = chart_path / "Chart.yaml"
    chart_lock = chart_path / "Chart.lock"
    charts_dir = chart_path / "charts"
    try:
        if not chart_yaml.is_file() or not chart_lock.is_file() or not charts_dir.is_dir():
            return False
        if chart_lock.stat().st_mtime < chart_yaml.stat().st_mtime:
            return False
        declared = load_chart_metadata(chart_yaml).dependencies
    except (OSError, SpecError):
        return False

    locked = _load_lock_dependencies(chart_lock)
    if locked is None or not _lock_matches_declaration(declared, locked):
        return False

    expected = {_identity(dependency) for dependency in locked}
    if None in expected:
        return False
    expected_identities = {identity for identity in expected if identity is not None}

    materialized = _materialized_identities(charts_dir)
    return materialized is not None and materialized == expected_identities


def _load_lock_dependencies(lock_path: Path) -> tuple[ChartDependency, ...] | None:
    """Strictly parse the dependency identity fields in Chart.lock."""
    try:
        data = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    dependencies = data.get("dependencies")
    if not isinstance(dependencies, list):
        return None

    parsed: list[ChartDependency] = []
    for raw in dependencies:
        if not isinstance(raw, dict):
            return None
        dependency: dict[str, Any] = raw
        name = dependency.get("name")
        version = dependency.get("version")
        repository = dependency.get("repository")
        alias = dependency.get("alias")
        if not isinstance(name, str) or not name.strip():
            return None
        if not isinstance(version, str) or not version.strip():
            return None
        if repository is not None and not isinstance(repository, str):
            return None
        if alias is not None and (not isinstance(alias, str) or not alias.strip()):
            return None
        parsed.append(
            ChartDependency(
                name=name,
                version=version,
                repository=repository,
                alias=alias,
            )
        )
    return tuple(parsed)


def _lock_matches_declaration(
    declared: tuple[ChartDependency, ...],
    locked: tuple[ChartDependency, ...],
) -> bool:
    """Check names/cardinality while allowing lock-resolved version ranges.

    Helm aliases do not rename the packaged chart: the artifact's Chart.yaml
    still contains the dependency's real name. Multiple declarations of one
    chart are valid only when each occurrence has a distinct alias; Helm may
    materialize their shared package only once.
    """
    if Counter(item.name for item in declared) != Counter(item.name for item in locked):
        return False
    if any(item.alias is not None for item in locked) and Counter(
        (item.name, item.alias) for item in declared
    ) != Counter((item.name, item.alias) for item in locked):
        return False

    declared_by_name: dict[str, list[ChartDependency]] = {}
    for dependency in declared:
        declared_by_name.setdefault(dependency.name, []).append(dependency)
    for dependencies in declared_by_name.values():
        if len(dependencies) < 2:
            continue
        aliases = [dependency.alias for dependency in dependencies]
        if any(alias is None for alias in aliases) or len(set(aliases)) != len(aliases):
            return False
    return True


def _identity(dependency: ChartDependency) -> _DependencyIdentity | None:
    if dependency.version is None or not dependency.version.strip():
        return None
    return _DependencyIdentity(dependency.name, dependency.version)


def _materialized_identities(
    charts_dir: Path,
) -> set[_DependencyIdentity] | None:
    identities: set[_DependencyIdentity] = set()
    try:
        entries = tuple(charts_dir.iterdir())
    except OSError:
        return None

    for entry in entries:
        identity: _DependencyIdentity | None
        try:
            if entry.is_symlink():
                if entry.suffix == ".tgz" or entry.is_dir():
                    return None
                continue
            if entry.suffix == ".tgz":
                if not entry.is_file():
                    return None
                identity = _packaged_chart_identity(entry)
            elif entry.is_dir():
                metadata_path = entry / "Chart.yaml"
                if not metadata_path.exists():
                    # Generated/cache directories are not chart artifacts.
                    continue
                metadata = load_chart_metadata(metadata_path)
                identity = _metadata_identity(metadata.name, metadata.version)
            else:
                continue
        except (OSError, SpecError):
            return None
        if identity is None or identity in identities:
            return None
        identities.add(identity)
    return identities


def _metadata_identity(name: object, version: object) -> _DependencyIdentity | None:
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(version, str) or not version.strip():
        return None
    return _DependencyIdentity(name, version)


def _packaged_chart_identity(path: Path) -> _DependencyIdentity | None:
    """Read one packaged Chart.yaml without extracting archive contents."""
    try:
        if path.stat().st_size > _MAX_ARCHIVE_BYTES:
            return None
        candidates: list[_DependencyIdentity] = []
        with tarfile.open(path, mode="r|gz") as archive:
            for index, member in enumerate(archive):
                if index >= _MAX_ARCHIVE_MEMBERS:
                    return None
                if member.offset_data + member.size > _MAX_ARCHIVE_OFFSET:
                    return None

                member_path = PurePosixPath(member.name)
                if (
                    len(member_path.parts) != 2
                    or member_path.parts[1] != "Chart.yaml"
                    or member_path.is_absolute()
                    or ".." in member_path.parts
                ):
                    continue
                if (
                    not member.isfile()
                    or member.size < 0
                    or member.size > _MAX_CHART_YAML_BYTES
                ):
                    return None
                stream = archive.extractfile(member)
                if stream is None:
                    return None
                raw = stream.read(_MAX_CHART_YAML_BYTES + 1)
                if len(raw) > _MAX_CHART_YAML_BYTES:
                    return None
                candidates.append(_identity_from_chart_yaml(raw))
    except (OSError, EOFError, tarfile.TarError, UnicodeError, yaml.YAMLError):
        return None

    if len(candidates) != 1:
        return None
    return candidates[0]


def _identity_from_chart_yaml(raw: bytes) -> _DependencyIdentity:
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise yaml.YAMLError("packaged Chart.yaml must contain a mapping")
    identity = _metadata_identity(data.get("name"), data.get("version"))
    if identity is None:
        raise yaml.YAMLError("packaged Chart.yaml has invalid name or version")
    return identity
