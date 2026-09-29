"""Immutable XDG schema generations and their transactional publication."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from chart_manager.services.schemas.errors import SchemaStoreError
from chart_manager.services.schemas.lock import write_schema_lock_atomic
from chart_manager.services.schemas.models import (
    GroupVersionKind,
    SchemaFile,
    SchemaLock,
    SchemaScope,
    content_digest,
)


@dataclass(frozen=True)
class StoreProblem:
    path: str
    detail: str


@dataclass(frozen=True)
class StoreStatus:
    generation_path: Path
    expected: int
    present: int
    missing: tuple[StoreProblem, ...]
    corrupt: tuple[StoreProblem, ...]
    uncovered: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.missing and not self.corrupt and not self.uncovered


@dataclass(frozen=True)
class KubeconformSchemaLocations:
    """Insertion points around chart-authored lifecycle additions."""

    generated_schema_locations: tuple[str, ...]
    fallback_schema_locations: tuple[str, ...]


def default_schema_cache_root() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise SchemaStoreError("XDG_CACHE_HOME must be an absolute path")
        return path / "chart-manager" / "schemas"
    return Path.home() / ".cache" / "chart-manager" / "schemas"


def schema_relative_path(entry: SchemaFile) -> Path:
    """Return the model-validated relative path carried by a lock entry."""
    return Path(*entry.path.split("/"))


def artifact_relative_path(
    *,
    source: str,
    gvk: GroupVersionKind,
    scope: SchemaScope | None,
) -> str:
    """Choose a deterministic collision-free store path for one schema."""
    name = f"{gvk.kind.lower()}_{gvk.version}.json"
    if source in {"generated", "local"}:
        if scope is None:
            raise SchemaStoreError(f"{source} schema {gvk.key} requires a scope")
        environment = scope.environment or "_all"
        return (Path(source) / scope.chart / environment / gvk.group / name).as_posix()
    if source == "catalog":
        return (Path("catalog") / gvk.group / name).as_posix()
    if source == "kubernetes":
        # An empty core group intentionally contributes no path segment. The
        # corresponding kubeconform template contains `.../{{.Group}}/...`;
        # its harmless double slash resolves to this path for core/v1 objects.
        return (Path("kubernetes") / gvk.group / name).as_posix()
    raise SchemaStoreError(f"unknown schema source: {source}")


class SchemaStore:
    """Manage immutable content-addressed generations for one workspace."""

    def __init__(self, workspace: str, *, cache_root: Path | None = None) -> None:
        if not workspace or any(character in workspace for character in "/\\"):
            raise SchemaStoreError("workspace must be a non-empty path-safe name")
        self.workspace = workspace
        self.cache_root = (cache_root or default_schema_cache_root()).resolve()
        self.root = self.cache_root / workspace
        self._lock_path = self.root / ".sync.lock"

    def generation_path(self, lock_or_digest: SchemaLock | str) -> Path:
        digest = (
            lock_or_digest.generation
            if isinstance(lock_or_digest, SchemaLock)
            else lock_or_digest
        )
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise SchemaStoreError(f"invalid generation digest: {digest}")
        return self.root / digest.removeprefix("sha256:")

    def create_stage(self) -> Path:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
        except OSError as exc:
            raise SchemaStoreError(f"failed to create schema staging directory: {exc}") from exc

    def write_stage_file(self, stage: Path, entry: SchemaFile, content: bytes) -> Path:
        self._require_stage(stage)
        if content_digest(content) != entry.sha256:
            raise SchemaStoreError(f"content checksum does not match lock entry {entry.path}")
        destination = (stage / schema_relative_path(entry)).resolve()
        if not destination.is_relative_to(stage.resolve()):
            raise SchemaStoreError(f"schema path escapes staged generation: {entry.path}")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        except OSError as exc:
            raise SchemaStoreError(f"failed to stage schema {entry.path}: {exc}") from exc
        return destination

    def inspect(self, lock: SchemaLock, *, root: Path | None = None) -> StoreStatus:
        generation = (root or self.generation_path(lock)).resolve()
        expected = len(lock.schemas)
        expected_paths = {entry.path for entry in lock.schemas}
        present = 0
        missing: list[StoreProblem] = []
        corrupt: list[StoreProblem] = []
        for entry in lock.schemas:
            path = generation / schema_relative_path(entry)
            try:
                resolved = path.resolve(strict=True)
            except FileNotFoundError:
                missing.append(StoreProblem(entry.path, "file is missing"))
                continue
            except OSError as exc:
                corrupt.append(StoreProblem(entry.path, f"cannot resolve file: {exc}"))
                continue
            if not resolved.is_relative_to(generation) or not resolved.is_file():
                corrupt.append(StoreProblem(entry.path, "path is not a regular in-generation file"))
                continue
            try:
                content = resolved.read_bytes()
            except OSError as exc:
                corrupt.append(StoreProblem(entry.path, f"cannot read file: {exc}"))
                continue
            present += 1
            actual = content_digest(content)
            if actual != entry.sha256:
                corrupt.append(
                    StoreProblem(entry.path, f"checksum is {actual}, expected {entry.sha256}")
                )
                continue
            try:
                decoded = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                corrupt.append(StoreProblem(entry.path, f"invalid JSON: {exc}"))
                continue
            if not isinstance(decoded, dict):
                corrupt.append(StoreProblem(entry.path, "schema JSON must be an object"))

        if generation.is_dir():
            try:
                candidates = tuple(generation.rglob("*"))
            except OSError as exc:
                corrupt.append(StoreProblem(".", f"cannot enumerate generation: {exc}"))
            else:
                for candidate in candidates:
                    if candidate.is_dir() and not candidate.is_symlink():
                        continue
                    relative = candidate.relative_to(generation).as_posix()
                    if relative not in expected_paths:
                        corrupt.append(StoreProblem(relative, "file is not declared by the lock"))

        uncovered = tuple(
            sorted(
                f"{requirement.scope.key}: {requirement.gvk.key}"
                for requirement in lock.inventory
                if not requirement.allow_missing
                and not any(
                    _entry_covers(entry, requirement.gvk, requirement.scope)
                    for entry in lock.schemas
                )
            )
        )
        return StoreStatus(
            generation_path=generation,
            expected=expected,
            present=present,
            missing=tuple(missing),
            corrupt=tuple(corrupt),
            uncovered=uncovered,
        )

    def require_ready(self, lock: SchemaLock, *, root: Path | None = None) -> Path:
        status = self.inspect(lock, root=root)
        if not status.ready:
            details = [
                *(f"missing {problem.path}: {problem.detail}" for problem in status.missing),
                *(f"corrupt {problem.path}: {problem.detail}" for problem in status.corrupt),
                *(f"uncovered {item}" for item in status.uncovered),
            ]
            raise SchemaStoreError(
                f"schema generation {lock.generation} is not ready: " + "; ".join(details)
            )
        return status.generation_path

    def publish_generation(self, stage: Path, lock: SchemaLock) -> Path:
        """Verify and atomically publish a staged immutable generation."""
        self._require_stage(stage)
        self.require_ready(lock, root=stage)
        destination = self.generation_path(lock)
        if destination.exists():
            # A concurrent or previous sync won. It is usable only if it matches.
            self.require_ready(lock, root=destination)
            try:
                shutil.rmtree(stage)
            except OSError as exc:
                raise SchemaStoreError(
                    f"published generation is ready but staging cleanup failed: {exc}"
                ) from exc
            return destination
        try:
            stage.rename(destination)
            _fsync_directory(self.root)
        except OSError as exc:
            if destination.exists():
                self.require_ready(lock, root=destination)
                shutil.rmtree(stage, ignore_errors=True)
                return destination
            raise SchemaStoreError(f"failed to publish schema generation: {exc}") from exc
        return destination

    def publish_transaction(
        self,
        stage: Path,
        lock: SchemaLock,
        *,
        lock_path: Path | None,
    ) -> Path:
        """Publish generation first, then atomically advance the optional repo lock."""
        with self.serialized_sync():
            destination = self.publish_generation(stage, lock)
            if lock_path is not None:
                write_schema_lock_atomic(lock_path, lock)
            return destination

    @contextmanager
    def serialized_sync(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock_path.open("a+b") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise SchemaStoreError(f"failed to lock schema store {self._lock_path}: {exc}") from exc

    def discard_stage(self, stage: Path) -> None:
        try:
            self._require_stage(stage)
        except SchemaStoreError:
            return
        shutil.rmtree(stage, ignore_errors=True)

    def _require_stage(self, stage: Path) -> None:
        resolved = stage.resolve()
        if (
            resolved.parent != self.root.resolve()
            or not resolved.name.startswith(".staging-")
            or not resolved.is_dir()
        ):
            raise SchemaStoreError(f"not a schema-store staging directory: {stage}")


def kubeconform_schema_locations(
    lock: SchemaLock,
    generation_path: Path,
    *,
    scope: SchemaScope,
) -> KubeconformSchemaLocations:
    """Return local templates split around lifecycle-authored additions."""
    root = generation_path.resolve()
    preferred: list[str] = []
    fallback: list[str] = []
    for source in ("generated", "local"):
        target = preferred if source == "generated" else fallback
        for environment in (scope.environment or "", ""):
            selected_scope = SchemaScope(
                chart=scope.chart,
                environment=environment or None,
            )
            if not any(
                entry.source == source and entry.scope == selected_scope
                for entry in lock.schemas
            ):
                continue
            base = root / source / scope.chart / (environment or "_all")
            target.append(
                str(base / "{{.Group}}" / "{{.ResourceKind}}_{{.ResourceAPIVersion}}.json")
            )
    for source in ("kubernetes", "catalog"):
        if not any(entry.source == source for entry in lock.schemas):
            continue
        fallback.append(
            str(root / source / "{{.Group}}" / "{{.ResourceKind}}_{{.ResourceAPIVersion}}.json")
        )
    return KubeconformSchemaLocations(
        generated_schema_locations=tuple(preferred),
        fallback_schema_locations=tuple(fallback),
    )


def _entry_covers(entry: SchemaFile, gvk: GroupVersionKind, scope: SchemaScope) -> bool:
    if entry.gvk != gvk:
        return False
    return entry.scope is None or entry.scope.covers(scope)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "KubeconformSchemaLocations",
    "SchemaStore",
    "StoreProblem",
    "StoreStatus",
    "artifact_relative_path",
    "default_schema_cache_root",
    "kubeconform_schema_locations",
    "schema_relative_path",
]
