"""Repository policy adapter for upstream schema snapshot synchronization."""

from __future__ import annotations

from pathlib import Path

from chart_manager.domain.workspace import SCHEMA_LOCK_FILE, RepositoryWorkspace
from chart_manager.services.kubeconform_schemas.errors import KubeconformSchemaConfigurationError
from chart_manager.services.kubeconform_schemas.models import AuthoredSchemaPolicy
from chart_manager.services.kubeconform_schemas.source import KubeconformSchemaSource
from chart_manager.services.kubeconform_schemas.store import KubeconformSchemaStore
from chart_manager.services.kubeconform_schemas.sync import (
    KubeconformSchemaSyncRequest,
    KubeconformSchemaSyncResult,
    KubeconformSchemaSyncService,
)


class RepositoryKubeconformSchemaService:
    def __init__(
        self, *, workspace: RepositoryWorkspace, sync: KubeconformSchemaSyncService
    ) -> None:
        self.workspace = workspace
        self.sync_service = sync

    def sync(self, *, update: bool = False) -> KubeconformSchemaSyncResult:
        policy = self.workspace.validation
        if policy is None or not self.workspace.name:
            raise KubeconformSchemaConfigurationError(
                f"{self.workspace.marker} must declare metadata.name and spec.validation "
                "before schemas can be synchronized"
            )
        return self.sync_service.sync(
            KubeconformSchemaSyncRequest(
                workspace=self.workspace.name,
                policy=AuthoredSchemaPolicy(
                    kubernetes_version=policy.kubernetes_version,
                    generate_from_crds=policy.schemas.generate_from_crds,
                    catalog_repository=policy.schemas.catalog.repository,
                    catalog_track=policy.schemas.catalog.track,
                ),
                lock_path=self.workspace.root / SCHEMA_LOCK_FILE,
                update=update,
            )
        )


def build_repository_kubeconform_schema_service(
    *,
    workspace: RepositoryWorkspace,
    source: KubeconformSchemaSource,
    cache_root: Path | None = None,
) -> RepositoryKubeconformSchemaService:
    return RepositoryKubeconformSchemaService(
        workspace=workspace,
        sync=KubeconformSchemaSyncService(
            KubeconformSchemaStore(workspace.name or "", cache_root=cache_root), source
        ),
    )
