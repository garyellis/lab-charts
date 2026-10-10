"""The committed schema lock: load, write, and sync the store to it."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaLockError,
)
from chart_manager.commands.validate.schemas.models import (
    AuthoredSchemaPolicy,
    LockedSchemaPolicy,
    RepositoryPin,
    SchemaLock,
    build_lock,
    lock_policy_mismatches,
)
from chart_manager.commands.validate.schemas.store import KubeconformSchemaStore
from chart_manager.plumbing.errors import YamlError
from chart_manager.plumbing.yaml_files import dump_yaml, load_yaml_file
from chart_manager.shared.workspace import SCHEMA_LOCK_FILE, RepositoryWorkspace


@dataclass(frozen=True)
class SyncResult:
    lock: SchemaLock
    generation_path: Path
    published: bool


def sync(workspace: RepositoryWorkspace, store: KubeconformSchemaStore) -> SyncResult:
    """Cache the upstream schema repositories at the committed pins."""
    policy = _policy(workspace)
    path = workspace.root / SCHEMA_LOCK_FILE
    if not path.is_file():
        raise KubeconformSchemaLockError(
            "schema lock is missing; run `chart-manager schemas sync --update`"
        )
    lock = load_schema_lock(path)
    mismatches = lock_policy_mismatches(policy, lock, workspace=workspace.name)
    if mismatches:
        raise KubeconformSchemaLockError(
            "schema policy differs from lock; run "
            "`chart-manager schemas sync --update`: " + "; ".join(mismatches)
        )
    published = store.sync(lock)
    return SyncResult(lock, store.generation_path(), published)


def update(
    workspace: RepositoryWorkspace,
    store: KubeconformSchemaStore,
    *,
    resolve_ref: Callable[[str, str], str],
) -> SyncResult:
    """Resolve both tracking refs to new pins, cache them, then write the lock."""
    policy = _policy(workspace)
    lock = build_lock(
        workspace=workspace.name,
        policy=LockedSchemaPolicy(
            kubernetes_version=policy.normalized_version(),
            generate_from_crds=policy.generate_from_crds,
            kubernetes=RepositoryPin(
                repository=policy.kubernetes_repository,
                track=policy.kubernetes_track,
                resolved=resolve_ref(policy.kubernetes_repository, policy.kubernetes_track),
            ),
            catalog=RepositoryPin(
                repository=policy.catalog_repository,
                track=policy.catalog_track,
                resolved=resolve_ref(policy.catalog_repository, policy.catalog_track),
            ),
        ),
    )
    published = store.sync(lock)
    write_schema_lock_atomic(workspace.root / SCHEMA_LOCK_FILE, lock)
    return SyncResult(lock, store.generation_path(), published)


def _policy(workspace: RepositoryWorkspace) -> AuthoredSchemaPolicy:
    policy = workspace.spec.validation
    if policy is None:
        raise KubeconformSchemaConfigurationError(
            f"{workspace.marker} must declare spec.validation before schemas can be synchronized"
        )
    return AuthoredSchemaPolicy(
        kubernetes_version=policy.kubernetes_version,
        generate_from_crds=policy.schemas.generate_from_crds,
        catalog_repository=policy.schemas.catalog.repository,
        catalog_track=policy.schemas.catalog.track,
    )


def load_schema_lock(path: Path) -> SchemaLock:
    """Load one strict lock document and verify its content-derived generation id."""
    try:
        document = load_yaml_file(path)
        return SchemaLock.model_validate(document)
    except (OSError, ValidationError, ValueError, YamlError) as exc:
        raise KubeconformSchemaLockError(f"invalid schema lock {path}: {exc}") from exc


def serialize_schema_lock(lock: SchemaLock) -> str:
    """Return stable YAML; the model has already enforced canonical ordering."""
    try:
        return dump_yaml(lock.model_dump(mode="json", by_alias=True, exclude_none=True))
    except YamlError as exc:
        raise KubeconformSchemaLockError(f"failed to serialize schema lock: {exc}") from exc


def write_schema_lock_atomic(path: Path, lock: SchemaLock) -> None:
    """Atomically replace a lock file after its complete bytes are durable."""
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = serialize_schema_lock(lock).encode()
        fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except OSError as exc:
        raise KubeconformSchemaLockError(
            f"failed to atomically write schema lock {path}: {exc}"
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry where the platform permits directory fsync."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # The atomic rename remains valid on filesystems that reject directory fsync.
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "SCHEMA_LOCK_FILE",
    "SyncResult",
    "load_schema_lock",
    "serialize_schema_lock",
    "sync",
    "update",
    "write_schema_lock_atomic",
]
