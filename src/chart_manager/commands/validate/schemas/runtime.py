"""Read-only loading of one locked schema generation for validation."""

from __future__ import annotations

from dataclasses import dataclass

from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaLockError,
    KubeconformSchemaSourceEnvironmentError,
    KubeconformSchemaStoreError,
)
from chart_manager.commands.validate.schemas.lock import load_schema_lock
from chart_manager.commands.validate.schemas.models import (
    AuthoredSchemaPolicy,
    SchemaLock,
    lock_policy_mismatches,
)
from chart_manager.commands.validate.schemas.store import (
    KubeconformSchemaLocations,
    KubeconformSchemaStore,
)
from chart_manager.shared.workspace import SCHEMA_LOCK_FILE, RepositoryWorkspace

UNSUPPORTED_CRD_OBJECT_GVK = "apiextensions.k8s.io/v1/CustomResourceDefinition"


@dataclass(frozen=True)
class KubeconformSchemaRuntime:
    """Verified immutable generation shared by every row in one validate run."""

    lock: SchemaLock
    store: KubeconformSchemaStore
    generated_schema_locations: tuple[str, ...] = ()

    def locations(self) -> KubeconformSchemaLocations:
        locations = self.store.locations(self.lock)
        return KubeconformSchemaLocations(
            self.generated_schema_locations, locations.fallback_schema_locations
        )

    def ignored_missing_kinds(self) -> tuple[str, ...]:
        """Return the one exact upstream schema gap handled by validation.

        The pinned Kubernetes schema repository exposes CRD component
        definitions, but no top-level schema for a CRD object. Rendered CRD
        definitions still generate managed schemas for their custom resources.
        The runner skips this GVK only when no managed schema file exists.
        """
        return (UNSUPPORTED_CRD_OBJECT_GVK,)


def load_kubeconform_schema_runtime(
    workspace: RepositoryWorkspace,
    store: KubeconformSchemaStore,
) -> KubeconformSchemaRuntime:
    """Verify policy, lock and cache without network access or filesystem writes."""
    authored = workspace.spec.validation
    if authored is None:
        raise KubeconformSchemaConfigurationError(
            f"{workspace.marker} has no spec.validation schema policy"
        )
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
        workspace=workspace.name,
    )
    if mismatches:
        raise KubeconformSchemaLockError(
            "schema lock does not match workspace policy; run "
            "`chart-manager schemas sync --update`: " + "; ".join(mismatches)
        )

    status = store.inspect(lock)
    if status.corrupt:
        details = "; ".join(f"{problem.path}: {problem.detail}" for problem in status.corrupt)
        raise KubeconformSchemaStoreError(
            f"schema generation {lock.generation} is corrupt: {details}; "
            "remove the affected snapshot above, then run "
            "`chart-manager schemas sync`"
        )
    if status.missing:
        detail_parts = [f"missing {problem.path}" for problem in status.missing]
        raise KubeconformSchemaSourceEnvironmentError(
            f"schema generation {lock.generation} is not cached: "
            + "; ".join(detail_parts)
            + "; run `chart-manager schemas sync`"
        )
    return KubeconformSchemaRuntime(lock=lock, store=store)


__all__ = [
    "UNSUPPORTED_CRD_OBJECT_GVK",
    "KubeconformSchemaRuntime",
    "load_kubeconform_schema_runtime",
]
