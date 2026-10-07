"""Synchronize repository snapshots; only an explicit update advances pins."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaLockError,
    KubeconformSchemaSourceEnvironmentError,
)
from chart_manager.commands.validate.schemas.lock import (
    load_schema_lock,
    write_schema_lock_atomic,
)
from chart_manager.commands.validate.schemas.models import (
    AuthoredSchemaPolicy,
    LockedSchemaPolicy,
    RepositoryPin,
    SchemaLock,
    build_lock,
    lock_policy_mismatches,
)
from chart_manager.commands.validate.schemas.source import KubeconformSchemaSource
from chart_manager.commands.validate.schemas.store import KubeconformSchemaStore
from chart_manager.integrations.kubeconform.github_schema_source import (
    GitHubKubeconformSchemaSourceError,
)


@dataclass(frozen=True)
class KubeconformSchemaSyncRequest:
    workspace: str
    policy: AuthoredSchemaPolicy
    lock_path: Path
    update: bool = False


@dataclass(frozen=True)
class KubeconformSchemaSyncResult:
    lock: SchemaLock
    generation_path: Path
    lock_updated: bool
    generation_published: bool


def sync(
    request: KubeconformSchemaSyncRequest,
    *,
    store: KubeconformSchemaStore,
    source: KubeconformSchemaSource,
) -> KubeconformSchemaSyncResult:
    if request.update:
        policy = request.policy
        try:
            kubernetes = RepositoryPin(
                repository=policy.kubernetes_repository,
                track=policy.kubernetes_track,
                resolved=source.resolve_ref(policy.kubernetes_repository, policy.kubernetes_track),
            )
            catalog = RepositoryPin(
                repository=policy.catalog_repository,
                track=policy.catalog_track,
                resolved=source.resolve_ref(policy.catalog_repository, policy.catalog_track),
            )
        except GitHubKubeconformSchemaSourceError as exc:
            raise KubeconformSchemaSourceEnvironmentError(str(exc)) from exc
        lock = build_lock(
            workspace=request.workspace,
            policy=LockedSchemaPolicy(
                kubernetes_version=policy.normalized_version(),
                generate_from_crds=policy.generate_from_crds,
                kubernetes=kubernetes,
                catalog=catalog,
            ),
        )
    else:
        if not request.lock_path.is_file():
            raise KubeconformSchemaLockError(
                "schema lock is missing; run `chart-manager schemas sync --update`"
            )
        lock = load_schema_lock(request.lock_path)
        mismatches = lock_policy_mismatches(request.policy, lock, workspace=request.workspace)
        if mismatches:
            raise KubeconformSchemaLockError(
                "schema policy differs from lock; run "
                "`chart-manager schemas sync --update`: " + "; ".join(mismatches)
            )
    published = store.sync(lock)
    if request.update:
        write_schema_lock_atomic(request.lock_path, lock)
    return KubeconformSchemaSyncResult(lock, store.generation_path(), request.update, published)
