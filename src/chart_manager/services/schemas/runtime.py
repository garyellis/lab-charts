"""Read-only loading of one locked schema generation for validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.domain.workspace import SCHEMA_LOCK_FILE, RepositoryWorkspace
from chart_manager.services.schemas.errors import (
    SchemaConfigurationError,
    SchemaLockError,
    SchemaSourceEnvironmentError,
    SchemaStoreError,
)
from chart_manager.services.schemas.lock import load_schema_lock
from chart_manager.services.schemas.models import SchemaLock, SchemaScope
from chart_manager.services.schemas.store import (
    KubeconformSchemaLocations,
    SchemaStore,
    kubeconform_schema_locations,
)


@dataclass(frozen=True)
class SchemaRuntime:
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
        """Return the locked per-scope allow-list as kubeconform Kind skips."""
        return tuple(
            sorted(
                {
                    requirement.gvk.kind
                    for requirement in self.lock.inventory
                    if requirement.scope == scope and requirement.allow_missing
                }
            )
        )


def load_schema_runtime(
    workspace: RepositoryWorkspace,
    *,
    cache_root: Path | None = None,
) -> SchemaRuntime:
    """Verify policy, lock and cache without network access or filesystem writes."""
    authored = workspace.validation
    if authored is None:
        raise SchemaConfigurationError(
            f"{workspace.marker} has no spec.validation schema policy"
        )
    if not workspace.name:
        raise SchemaConfigurationError(
            f"{workspace.marker} must declare metadata.name before schemas can be loaded"
        )
    workspace_name = workspace.name
    path = workspace.root / SCHEMA_LOCK_FILE
    if not path.is_file():
        raise SchemaLockError(
            f"schema lock does not exist: {path}; run "
            "`chart-manager schemas sync --update`"
        )
    lock = load_schema_lock(path)
    mismatches: list[str] = []
    expected = (
        ("workspace", lock.workspace, workspace_name),
        ("kubernetesVersion", lock.policy.kubernetes_version, authored.kubernetes_version),
        (
            "generateFromCRDs",
            lock.policy.generate_from_crds,
            authored.schemas.generate_from_crds,
        ),
        (
            "catalog.repository",
            lock.policy.catalog.repository,
            authored.schemas.catalog.repository,
        ),
        ("catalog.track", lock.policy.catalog.track, authored.schemas.catalog.track),
    )
    for name, actual, wanted in expected:
        if actual != wanted:
            mismatches.append(f"{name}: lock={actual!r}, workspace={wanted!r}")
    if mismatches:
        raise SchemaLockError(
            "schema lock does not match workspace policy; run "
            "`chart-manager schemas sync --update`: " + "; ".join(mismatches)
        )

    status = SchemaStore(workspace_name, cache_root=cache_root).inspect(lock)
    if status.corrupt:
        details = "; ".join(
            f"{problem.path}: {problem.detail}" for problem in status.corrupt
        )
        raise SchemaStoreError(
            f"schema generation {lock.generation} is corrupt: {details}; "
            "run `chart-manager schemas sync`"
        )
    if status.missing or status.uncovered:
        detail_parts = [
            *(f"missing {problem.path}" for problem in status.missing),
            *(f"uncovered {item}" for item in status.uncovered),
        ]
        raise SchemaSourceEnvironmentError(
            f"schema generation {lock.generation} is not cached: "
            + "; ".join(detail_parts)
            + "; run `chart-manager schemas sync` while online"
        )
    return SchemaRuntime(lock=lock, generation_path=status.generation_path)


__all__ = ["SchemaRuntime", "load_schema_runtime"]
