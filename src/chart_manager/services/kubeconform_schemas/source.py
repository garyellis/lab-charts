"""Port used by schema synchronization to obtain immutable artifacts."""

from __future__ import annotations

from typing import Protocol

from chart_manager.integrations.kubeconform.github_schema_source import (
    KubeconformSchemaArtifactBatch,
    KubeconformSchemaArtifactRequest,
)


class KubeconformSchemaSource(Protocol):
    """Source operations required by kubeconform schema synchronization."""

    def resolve_ref(self, repository: str, ref: str) -> str:
        """Resolve a repository tracking ref to an immutable revision."""
        ...

    def artifact_url(self, repository: str, revision: str, path: str) -> str:
        """Build the immutable URL for an artifact within a repository."""
        ...

    def fetch_many(
        self,
        requests: list[KubeconformSchemaArtifactRequest]
        | tuple[KubeconformSchemaArtifactRequest, ...],
        *,
        allow_not_found: bool = False,
    ) -> KubeconformSchemaArtifactBatch:
        """Fetch requested artifacts with optional not-found collection."""
        ...


__all__ = [
    "KubeconformSchemaArtifactBatch",
    "KubeconformSchemaArtifactRequest",
    "KubeconformSchemaSource",
]
