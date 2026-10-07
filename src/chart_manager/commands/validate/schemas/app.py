"""`schemas sync`: cache the upstream schema repositories the workspace pins."""

from __future__ import annotations

from chart_manager.commands.validate.schemas import sync as schema_sync
from chart_manager.commands.validate.schemas.errors import KubeconformSchemaConfigurationError
from chart_manager.commands.validate.schemas.models import AuthoredSchemaPolicy
from chart_manager.commands.validate.schemas.source import KubeconformSchemaSource
from chart_manager.commands.validate.schemas.store import KubeconformSchemaStore
from chart_manager.shared.workspace import SCHEMA_LOCK_FILE, RepositoryWorkspace


def sync(
    workspace: RepositoryWorkspace,
    *,
    store: KubeconformSchemaStore,
    source: KubeconformSchemaSource,
    update: bool = False,
) -> schema_sync.KubeconformSchemaSyncResult:
    """Cache the upstream schema repositories at the workspace's pins; `update` moves the pins."""
    policy = workspace.spec.validation
    if policy is None:
        raise KubeconformSchemaConfigurationError(
            f"{workspace.marker} must declare spec.validation before schemas can be synchronized"
        )
    return schema_sync.sync(
        schema_sync.KubeconformSchemaSyncRequest(
            workspace=workspace.name,
            policy=AuthoredSchemaPolicy(
                kubernetes_version=policy.kubernetes_version,
                generate_from_crds=policy.schemas.generate_from_crds,
                catalog_repository=policy.schemas.catalog.repository,
                catalog_track=policy.schemas.catalog.track,
            ),
            lock_path=workspace.root / SCHEMA_LOCK_FILE,
            update=update,
        ),
        store=store,
        source=source,
    )
