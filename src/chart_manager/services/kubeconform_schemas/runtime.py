"""Read-only loading of one locked schema generation for validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.domain.workspace import SCHEMA_LOCK_FILE, RepositoryWorkspace
from chart_manager.services.kubeconform_schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaLockError,
    KubeconformSchemaSourceEnvironmentError,
    KubeconformSchemaStoreError,
)
from chart_manager.services.kubeconform_schemas.lock import load_schema_lock
from chart_manager.services.kubeconform_schemas.models import (
    AuthoredSchemaPolicy,
    SchemaLock,
    SchemaScope,
    lock_policy_mismatches,
)
from chart_manager.services.kubeconform_schemas.store import (
    KubeconformSchemaLocations,
    KubeconformSchemaStore,
    kubeconform_schema_locations,
)


@dataclass(frozen=True)
class KubeconformSchemaRuntime:
    """Verified immutable generation shared by every row in one validate run."""

    lock: SchemaLock
    generation_path: Path

    def locations(self, scope: SchemaScope) -> KubeconformSchemaLocations:
        return kubeconform_schema_locations(
            self.lock,
            self.generation_path,
            scope=scope,
        )

    def ignored_missing_kinds(self, scope: SchemaScope) -> tuple[str, ...]:
        """Compatibility hook; allow-missing policy is expanded from each row."""
        del scope
        return ()


def load_kubeconform_schema_runtime(
    workspace: RepositoryWorkspace,
    *,
    cache_root: Path | None = None,
) -> KubeconformSchemaRuntime:
    """Verify policy, lock and cache without network access or filesystem writes."""
    authored = workspace.validation
    if authored is None:
        raise KubeconformSchemaConfigurationError(
            f"{workspace.marker} has no spec.validation schema policy"
        )
    if not workspace.name:
        raise KubeconformSchemaConfigurationError(
            f"{workspace.marker} must declare metadata.name before schemas can be loaded"
        )
    workspace_name = workspace.name
    path = workspace.root / SCHEMA_LOCK_FILE
    if not path.is_file():
        raise KubeconformSchemaLockError(
            f"schema lock does not exist: {path}; run `chart-manager schemas sync --update`"
        )
    lock = load_schema_lock(path)
    mismatches = lock_policy_mismatches(
        AuthoredSchemaPolicy(
            kubernetes_version=authored.kubernetes_version,
            generate_from_crds=authored.schemas.generate_from_crds,
            catalog_repository=authored.schemas.catalog.repository,
            catalog_track=authored.schemas.catalog.track,
        ),
        lock,
        workspace=workspace_name,
    )
    if mismatches:
        raise KubeconformSchemaLockError(
            "schema lock does not match workspace policy; run "
            "`chart-manager schemas sync --update`: " + "; ".join(mismatches)
        )

    status = KubeconformSchemaStore(workspace_name, cache_root=cache_root).inspect(lock)
    if status.corrupt:
        details = "; ".join(f"{problem.path}: {problem.detail}" for problem in status.corrupt)
        raise KubeconformSchemaStoreError(
            f"schema generation {lock.generation} is corrupt: {details}; "
            f"remove {status.generation_path}, then run "
            "`chart-manager schemas sync` while online"
        )
    if status.uncovered:
        raise KubeconformSchemaLockError(
            f"schema lock does not cover its generation inventory: "
            f"{'; '.join(status.uncovered)}; run `chart-manager schemas sync --refresh`"
        )
    if status.missing:
        detail_parts = [f"missing {problem.path}" for problem in status.missing]
        raise KubeconformSchemaSourceEnvironmentError(
            f"schema generation {lock.generation} is not cached: "
            + "; ".join(detail_parts)
            + "; run `chart-manager schemas sync` while online"
        )
    return KubeconformSchemaRuntime(
        lock=lock,
        generation_path=status.generation_path,
    )


__all__ = ["KubeconformSchemaRuntime", "load_kubeconform_schema_runtime"]
