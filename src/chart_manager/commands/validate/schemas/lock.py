"""The committed schema lock: load, write, sync the store to it, and serve its locations."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from chart_manager.api.v1alpha1.chart_workspace import WorkspaceValidation
from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaError,
    KubeconformSchemaLockError,
    KubeconformSchemaSourceEnvironmentError,
    KubeconformSchemaStoreError,
)
from chart_manager.commands.validate.schemas.models import (
    LockedSchemaPolicy,
    RepositoryPin,
    SchemaLock,
    build_lock,
)
from chart_manager.commands.validate.schemas.store import KubeconformSchemaStore, StoreStatus
from chart_manager.plumbing.errors import YamlError
from chart_manager.plumbing.yaml_files import dump_yaml, load_yaml_file
from chart_manager.shared.workspace import SCHEMA_LOCK_FILE, RepositoryWorkspace

# The fixed upstream source of Kubernetes core schemas.
KUBERNETES_REPOSITORY = "yannh/kubernetes-json-schema"
KUBERNETES_TRACK = "master"

# The pinned Kubernetes schemas have no top-level schema for a CRD object, so
# validation skips this kind when no generated schema covers it.
UNSUPPORTED_CRD_OBJECT_GVK = "apiextensions.k8s.io/v1/CustomResourceDefinition"

_UPDATE = "run `chart-manager schemas sync --update`"
_SYNC = "run `chart-manager schemas sync`"


@dataclass(frozen=True)
class SyncResult:
    lock: SchemaLock
    generation_path: Path
    published: bool


@dataclass(frozen=True)
class UpstreamSchemas:
    """The locked upstream schemas every row of one validate run checks against."""

    kubernetes_version: str
    locations: tuple[str, ...]
    skip_kinds: tuple[str, ...]


def locations(workspace: RepositoryWorkspace, store: KubeconformSchemaStore) -> UpstreamSchemas:
    """Verify policy, lock and cache without network access or filesystem writes."""
    lock = _read(workspace)
    error = _store_error(lock, store.inspect(lock))
    if error:
        raise error
    return UpstreamSchemas(
        lock.policy.kubernetes_version, store.locations(lock), (UNSUPPORTED_CRD_OBJECT_GVK,)
    )


def sync(workspace: RepositoryWorkspace, store: KubeconformSchemaStore) -> SyncResult:
    """Cache the upstream schema repositories at the committed pins."""
    lock = _read(workspace)
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
    catalog = policy.schemas.catalog
    lock = build_lock(
        workspace=workspace.name,
        policy=LockedSchemaPolicy(
            kubernetes_version=policy.kubernetes_version,
            generate_from_crds=policy.schemas.generate_from_crds,
            kubernetes=RepositoryPin(
                repository=KUBERNETES_REPOSITORY,
                track=KUBERNETES_TRACK,
                resolved=resolve_ref(KUBERNETES_REPOSITORY, KUBERNETES_TRACK),
            ),
            catalog=RepositoryPin(
                repository=catalog.repository,
                track=catalog.track,
                resolved=resolve_ref(catalog.repository, catalog.track),
            ),
        ),
    )
    published = store.sync(lock)
    write_schema_lock_atomic(workspace.root / SCHEMA_LOCK_FILE, lock)
    return SyncResult(lock, store.generation_path(), published)


def _policy(workspace: RepositoryWorkspace) -> WorkspaceValidation:
    policy = workspace.spec.validation
    if policy is None:
        raise KubeconformSchemaConfigurationError(
            f"{workspace.marker} has no spec.validation schema policy"
        )
    return policy


def _read(workspace: RepositoryWorkspace) -> SchemaLock:
    """The committed lock, once it exists, loads and matches the workspace policy."""
    policy = _policy(workspace)
    lock = _load(workspace)
    mismatches = _mismatches(workspace.name, policy, lock)
    if mismatches:
        raise _mismatch_error(mismatches)
    return lock


def _load(workspace: RepositoryWorkspace) -> SchemaLock:
    path = workspace.root / SCHEMA_LOCK_FILE
    if not path.is_file():
        raise KubeconformSchemaLockError(f"schema lock does not exist: {path}; {_UPDATE}")
    return load_schema_lock(path)


def _mismatches(workspace: str, policy: WorkspaceValidation, lock: SchemaLock) -> tuple[str, ...]:
    """Compare every authored field that controls locked schema bytes."""
    locked = lock.policy
    comparisons: tuple[tuple[str, object, object], ...] = (
        ("workspace", lock.workspace, workspace),
        ("kubernetesVersion", locked.kubernetes_version, policy.kubernetes_version),
        ("generateFromCRDs", locked.generate_from_crds, policy.schemas.generate_from_crds),
        ("kubernetes.repository", locked.kubernetes.repository, KUBERNETES_REPOSITORY),
        ("kubernetes.track", locked.kubernetes.track, KUBERNETES_TRACK),
        ("catalog.repository", locked.catalog.repository, policy.schemas.catalog.repository),
        ("catalog.track", locked.catalog.track, policy.schemas.catalog.track),
    )
    return tuple(
        f"{name}: lock={value!r}, workspace={authored!r}"
        for name, value, authored in comparisons
        if value != authored
    )


def _mismatch_error(mismatches: tuple[str, ...]) -> KubeconformSchemaLockError:
    return KubeconformSchemaLockError(
        f"schema lock does not match workspace policy; {_UPDATE}: " + "; ".join(mismatches)
    )


def _store_error(lock: SchemaLock, status: StoreStatus) -> KubeconformSchemaError | None:
    """Why the store cannot serve the lock's generation, or None when it can."""
    if status.corrupt:
        details = "; ".join(f"{problem.path}: {problem.detail}" for problem in status.corrupt)
        return KubeconformSchemaStoreError(
            f"schema generation {lock.generation} is corrupt: {details}; "
            f"remove the affected snapshot above, then {_SYNC}"
        )
    if status.missing:
        missing = "; ".join(f"missing {problem.path}" for problem in status.missing)
        return KubeconformSchemaSourceEnvironmentError(
            f"schema generation {lock.generation} is not cached: {missing}; {_SYNC}"
        )
    return None


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
    "KUBERNETES_REPOSITORY",
    "KUBERNETES_TRACK",
    "SCHEMA_LOCK_FILE",
    "UNSUPPORTED_CRD_OBJECT_GVK",
    "SyncResult",
    "UpstreamSchemas",
    "load_schema_lock",
    "locations",
    "serialize_schema_lock",
    "sync",
    "update",
    "write_schema_lock_atomic",
]
