"""Ref resolution port; repository contents are fetched as pinned Git snapshots."""

from typing import Protocol


class KubeconformSchemaSource(Protocol):
    def resolve_ref(self, repository: str, ref: str) -> str:
        """Resolve a tracking ref only during an explicit upstream update."""
        ...
