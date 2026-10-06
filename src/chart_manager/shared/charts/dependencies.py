"""Conservative freshness policy for materialized Helm chart dependencies.

The functions in this module inspect Helm's ``Chart.yaml``, ``Chart.lock``,
and ``charts/`` state.  They never invoke Helm and never raise: uncertainty
means "stale", because a redundant dependency update is safer than rendering
with the wrong dependency.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from chart_manager.plumbing.errors import ChartManagerError, SpecError, YamlError
from chart_manager.plumbing.yaml_files import load_yaml_file, parse_yaml_mapping
from chart_manager.shared.charts.chart import (
    ChartDependency,
    ChartRepository,
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


@dataclass(frozen=True)
class _HelmDependency:
    """The fields and JSON order of Helm's ``chart.Dependency`` type."""

    name: str
    version: str
    repository: str
    condition: str = ""
    tags: tuple[str, ...] = ()
    enabled: bool = False
    import_values: tuple[Any, ...] = ()
    alias: str = ""

    def json_object(self) -> dict[str, Any]:
        """Return the Go ``encoding/json`` shape, including ``omitempty``."""
        value: dict[str, Any] = {
            "name": self.name,
        }
        if self.version:
            value["version"] = self.version
        # Repository deliberately has no omitempty tag in Helm.
        value["repository"] = self.repository
        if self.condition:
            value["condition"] = self.condition
        if self.tags:
            value["tags"] = list(self.tags)
        if self.enabled:
            value["enabled"] = True
        if self.import_values:
            value["import-values"] = list(self.import_values)
        if self.alias:
            value["alias"] = self.alias
        return value


_DEPENDENCY_FIELDS = frozenset(
    {
        "name",
        "version",
        "repository",
        "condition",
        "tags",
        "enabled",
        "import-values",
        "alias",
    }
)


def build_helm_dependency_index(
    root: Path, *, charts_dir: Path
) -> dict[str, set[str]]:
    """Map each local chart name to the managed charts that depend on it.

    Loads ordinary Helm chart metadata, so charts without enabled cluster
    tests — including library charts — still enter the index. Malformed
    charts are skipped by this best-effort repository-wide scan; explicitly
    requested charts remain strict.

    Only repository-less or ``file://`` dependencies are indexed, and a
    chart's dependency on its own name is skipped. Wrapper charts depend on
    a remote upstream of the same name, so indexing by name alone recorded
    ``grafana -> {grafana}`` and the planner selected every environment for
    any wrapper file edit, bypassing per-environment triggers. A remote
    dependency cannot change with a file in this repository, and a chart's
    own edits are planned by its triggers, so neither needs fanout.
    """
    index: dict[str, set[str]] = {}
    repository = ChartRepository(root, charts_dir=charts_dir)
    for name in repository.list_names():
        try:
            chart = repository.get(name)
        except ChartManagerError:
            continue
        for dependency in chart.metadata.dependencies:
            if dependency.name == chart.name or not _is_local_dependency(dependency):
                continue
            index.setdefault(dependency.name, set()).add(chart.name)
    return index


def _is_local_dependency(dependency: ChartDependency) -> bool:
    """Return whether Helm would resolve the dependency from local files.

    Helm reads an omitted/empty ``repository`` from the parent's ``charts/``
    directory and ``file://`` from a path; everything else (https, oci,
    ``@alias`` repositories) is fetched remotely.
    """
    repository = dependency.repository
    return not repository or repository.startswith("file://")


def deps_are_fresh(chart_path: Path) -> bool:
    """Return whether Helm's lock digest and materialized identities agree.

    A fresh result requires:

    * ``Chart.lock`` and ``charts/`` exist;
    * ``Chart.lock.digest`` equals Helm's content hash of the authored and
      resolved dependency arrays;
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
        chart_data = load_yaml_file(chart_yaml)
        lock_data = load_yaml_file(chart_lock)
    except (OSError, YamlError):
        return False

    declared = _parse_dependencies(chart_data, require_nonempty=True)
    locked = _parse_dependencies(lock_data, require_nonempty=True)
    digest = lock_data.get("digest")
    try:
        calculated_digest = (
            _helm_dependency_digest(declared, locked)
            if declared is not None and locked is not None
            else None
        )
    except (TypeError, UnicodeError, ValueError):
        return False
    if (
        declared is None
        or locked is None
        or not isinstance(digest, str)
        or digest != calculated_digest
    ):
        return False

    expected = {_identity(dependency) for dependency in locked}
    if None in expected:
        return False
    expected_identities = {identity for identity in expected if identity is not None}

    materialized = _materialized_identities(charts_dir)
    return materialized is not None and materialized == expected_identities


def _parse_dependencies(
    data: dict[str, Any], *, require_nonempty: bool
) -> tuple[_HelmDependency, ...] | None:
    """Parse exactly the dependency fields Helm includes in ``HashReq``."""
    dependencies = data.get("dependencies")
    if not isinstance(dependencies, list) or (require_nonempty and not dependencies):
        return None

    parsed: list[_HelmDependency] = []
    for raw in dependencies:
        if not isinstance(raw, dict) or not set(raw).issubset(_DEPENDENCY_FIELDS):
            return None
        dependency: dict[str, Any] = raw
        name = dependency.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        version = dependency.get("version", "")
        repository = dependency.get("repository", "")
        condition = dependency.get("condition", "")
        alias = dependency.get("alias", "")
        if not all(
            isinstance(value, str)
            for value in (version, repository, condition, alias)
        ):
            return None
        tags = dependency.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            return None
        enabled = dependency.get("enabled", False)
        if not isinstance(enabled, bool):
            return None
        import_values = dependency.get("import-values", [])
        if not _is_supported_import_values(import_values):
            return None
        sanitized_name = _helm_sanitize(name)
        sanitized_version = _helm_sanitize(version)
        if not sanitized_name.strip() or not sanitized_version.strip():
            return None
        parsed.append(
            _HelmDependency(
                name=sanitized_name,
                version=sanitized_version,
                repository=_helm_sanitize(repository),
                condition=_helm_sanitize(condition),
                tags=tuple(_helm_sanitize(tag) for tag in tags),
                enabled=enabled,
                import_values=tuple(
                    item
                    if isinstance(item, str)
                    else {"child": item["child"], "parent": item["parent"]}
                    for item in import_values
                ),
                alias=alias,
            )
        )
    return tuple(parsed)


def _helm_sanitize(value: str) -> str:
    """Mirror Helm's whitespace normalization and non-printable removal."""
    return "".join(
        " " if character.isspace() else character
        for character in value
        if character.isprintable() or character.isspace()
    )


def _is_supported_import_values(value: Any) -> bool:
    """Accept Helm's documented string or child/parent mapping entries."""
    if not isinstance(value, list):
        return False
    return all(
        isinstance(item, str)
        or (
            isinstance(item, dict)
            and set(item) == {"child", "parent"}
            and all(isinstance(part, str) for part in item.values())
        )
        for item in value
    )


def _helm_dependency_digest(
    declared: tuple[_HelmDependency, ...], locked: tuple[_HelmDependency, ...]
) -> str:
    """Reproduce Helm ``resolver.HashReq`` for supported dependency values."""
    value = [
        [dependency.json_object() for dependency in declared],
        [dependency.json_object() for dependency in locked],
    ]
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    # Go's encoding/json escapes HTML-significant characters and U+2028/U+2029.
    encoded = (
        encoded.replace(b"<", b"\\u003c")
        .replace(b">", b"\\u003e")
        .replace(b"&", b"\\u0026")
        .replace("\u2028".encode(), b"\\u2028")
        .replace("\u2029".encode(), b"\\u2029")
    )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _identity(dependency: ChartDependency | _HelmDependency) -> _DependencyIdentity | None:
    if not dependency.version or not dependency.version.strip():
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
        with (
            gzip.open(path, mode="rb") as compressed,
            tarfile.open(fileobj=compressed, mode="r|") as archive,
        ):
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
    except (OSError, EOFError, gzip.BadGzipFile, tarfile.TarError, YamlError):
        return None

    if len(candidates) != 1:
        return None
    return candidates[0]


def _identity_from_chart_yaml(raw: bytes) -> _DependencyIdentity:
    data = parse_yaml_mapping(raw, source="packaged Chart.yaml")
    identity = _metadata_identity(data.get("name"), data.get("version"))
    if identity is None:
        raise YamlError("packaged Chart.yaml has invalid name or version")
    return identity
