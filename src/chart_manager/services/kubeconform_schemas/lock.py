"""Load and deterministically serialize the committed schema lock."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from chart_manager.plumbing.errors import YamlError
from chart_manager.plumbing.yaml_files import dump_yaml, load_yaml_file
from chart_manager.services.kubeconform_schemas.errors import KubeconformSchemaLockError
from chart_manager.services.kubeconform_schemas.models import SchemaLock
from chart_manager.shared.workspace import SCHEMA_LOCK_FILE


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
    "load_schema_lock",
    "serialize_schema_lock",
    "write_schema_lock_atomic",
]
