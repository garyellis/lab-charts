"""The committed schema lock: load, write, sync the store to it, serve and preflight it."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from chart_manager.plumbing.errors import WorkspaceNotFoundError, YamlError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import Check, CheckStatus
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
_UPDATE_REMEDIATION = "run chart-manager schemas sync --update"
_SYNC_REMEDIATION = "run chart-manager schemas sync"


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


def preflight(
    workspace: RepositoryWorkspace | WorkspaceNotFoundError, store: KubeconformSchemaStore
) -> tuple[Check, ...]:
    """Report policy, lock and store readiness from disk only, without raising.

    Outside a workspace every check is skipped with the error's text.
    """
    if isinstance(workspace, WorkspaceNotFoundError):
        return tuple(
            Check.skipped(name, str(workspace))
            for name in ("schema-policy", "schema-lock", "schema-store")
        )
    try:
        policy = _policy(workspace)
    except KubeconformSchemaConfigurationError as error:
        unavailable = "workspace schema policy is unavailable"
        return (
            _failed(
                "schema-policy",
                error,
                "configure spec.validation.kubernetesVersion and "
                f"spec.validation.schemas, then {_UPDATE_REMEDIATION}",
                {"configured": False},
            ),
            Check.skipped("schema-lock", unavailable),
            Check.skipped("schema-store", unavailable),
        )
    policy_check = _policy_check(workspace.name, policy)
    lock_check, lock = _lock_check(workspace, policy)
    if lock is None:
        return (
            policy_check,
            lock_check,
            Check.skipped(
                "schema-store",
                "schema lock is unavailable; store generation cannot be selected",
                data={"ready": False},
            ),
        )
    try:
        status = store.inspect(lock)
    except OSError as exc:
        store_check = Check.failed(
            "schema-store",
            f"schema store cannot be inspected: {exc}",
            remediation=_SYNC_REMEDIATION,
            outcome=Outcome.ENVIRONMENT,
            data={"ready": False},
        )
    else:
        store_check = _store_check(lock, status, policy_matches=lock_check.status is CheckStatus.OK)
    return policy_check, lock_check, store_check


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


def _policy_check(workspace: str, policy: WorkspaceValidation) -> Check:
    catalog = policy.schemas.catalog
    detail = (
        f"workspace={workspace}; kubernetes={policy.kubernetes_version}; "
        f"generateFromCRDs={str(policy.schemas.generate_from_crds).lower()}; "
        f"catalog={catalog.repository}@{catalog.track}"
    )
    data = {
        "configured": True,
        "workspace": workspace,
        "kubernetesVersion": policy.kubernetes_version,
        "generateFromCRDs": policy.schemas.generate_from_crds,
        "kubernetes": {"repository": KUBERNETES_REPOSITORY, "track": KUBERNETES_TRACK},
        "catalog": {"repository": catalog.repository, "track": catalog.track},
    }
    return Check.ok("schema-policy", detail, data=data)


def _lock_check(
    workspace: RepositoryWorkspace, policy: WorkspaceValidation
) -> tuple[Check, SchemaLock | None]:
    """The lock check, and the lock when one loads, even if it mismatches the policy."""
    path = workspace.root / SCHEMA_LOCK_FILE
    try:
        lock = _load(workspace)
    except KubeconformSchemaLockError as error:
        unusable = {"path": str(path), "present": path.is_file(), "matchesPolicy": False}
        return _failed("schema-lock", error, _UPDATE_REMEDIATION, unusable), None
    mismatches = _mismatches(workspace.name, policy, lock)
    data: dict[str, Any] = {
        "path": str(path),
        "present": True,
        "workspace": lock.workspace,
        "generation": lock.generation,
        "matchesPolicy": not mismatches,
        "mismatches": list(mismatches),
        "repositories": 2,
    }
    if mismatches:
        return _failed("schema-lock", _mismatch_error(mismatches), _UPDATE_REMEDIATION, data), lock
    detail = f"{path}; generation={lock.generation}; policy matches; repositories=2"
    return Check.ok("schema-lock", detail, data=data), lock


def _store_check(lock: SchemaLock, status: StoreStatus, *, policy_matches: bool) -> Check:
    ready = status.ready and policy_matches
    data: dict[str, Any] = {
        "path": str(status.generation_path),
        "generation": lock.generation,
        "expected": status.expected,
        "present": status.present,
        "missing": len(status.missing),
        "corrupt": len(status.corrupt),
        "ready": ready,
        "generatedSchemas": "automatically prepared during validate",
    }
    error = _store_error(lock, status)
    if error is None:
        detail = (
            f"{status.generation_path}; generation={lock.generation}; "
            f"expected={status.expected} present={status.present} missing=0 corrupt=0; "
            f"ready={str(ready).lower()}; "
            "generated CRD schemas are prepared automatically during validate"
        )
        return Check.ok("schema-store", detail, data=data)
    remediation = _SYNC_REMEDIATION
    if status.corrupt:
        snapshots = "; ".join(problem.path for problem in status.corrupt)
        remediation = f"remove affected snapshot(s): {snapshots}, then {_SYNC_REMEDIATION}"
    return _failed("schema-store", error, remediation, data)


def _failed(
    name: str, error: KubeconformSchemaError, remediation: str, data: dict[str, Any]
) -> Check:
    return Check.failed(name, str(error), remediation=remediation, outcome=error.outcome, data=data)


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
    "preflight",
    "serialize_schema_lock",
    "sync",
    "update",
    "write_schema_lock_atomic",
]
