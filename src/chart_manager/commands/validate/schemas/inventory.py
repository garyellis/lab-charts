"""Collect CRDs with provenance from explicitly scoped rendered trees."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaIntegrityError,
)
from chart_manager.commands.validate.schemas.models import (
    GroupVersionKind,
    SchemaScope,
)
from chart_manager.plumbing.errors import YamlError
from chart_manager.plumbing.yaml_files import load_yaml_documents

# JSON is a YAML subset and Helm may preserve JSON-formatted CRDs in the
# rendered tree. Inspect every manifest format accepted by kubeconform so
# CRD discovery sees the same documents as validation.
_MANIFEST_SUFFIXES = frozenset({".json", ".yaml", ".yml"})


@dataclass(frozen=True)
class RenderedResource:
    """One rendered CRD document and its chart/environment provenance."""
    scope: SchemaScope
    document: dict[str, Any]
    path: Path
    document_index: int


def scan_rendered_directory(
    directory: Path,
    *,
    scope: SchemaScope,
) -> tuple[RenderedResource, ...]:
    """Collect CRDs from all documents under one rendered chart/environment directory."""
    root = directory.resolve()
    if not root.is_dir():
        raise KubeconformSchemaIntegrityError(f"rendered directory does not exist: {root}")
    resources: list[RenderedResource] = []
    for path in sorted(
        candidate
        for candidate in root.rglob("*")
        if candidate.is_file() and candidate.suffix.lower() in _MANIFEST_SUFFIXES
    ):
        try:
            documents = load_yaml_documents(path)
        except YamlError as exc:
            raise KubeconformSchemaConfigurationError(
                f"{scope.key}: failed to inventory rendered YAML {path}: {exc}"
            ) from exc
        for index, raw in enumerate(documents):
            for document in _resource_documents(raw, path=path, document_index=index, scope=scope):
                try:
                    gvk = GroupVersionKind.from_document(document)
                except (TypeError, ValueError) as exc:
                    raise KubeconformSchemaConfigurationError(
                        f"{scope.key}: invalid Kubernetes resource in "
                        f"{path} document {index + 1}: {exc}"
                    ) from exc
                if not _is_crd(gvk):
                    continue
                resources.append(
                    RenderedResource(
                        scope=scope,
                        document=document,
                        # Keep provider diagnostics independent of the machine's
                        # absolute render directory.
                        path=path.relative_to(root),
                        document_index=index,
                    )
                )
    return tuple(resources)


def _resource_documents(
    raw: Any,
    *,
    path: Path,
    document_index: int,
    scope: SchemaScope,
) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise KubeconformSchemaConfigurationError(
            f"{scope.key}: rendered YAML {path} document {document_index + 1} must be a mapping"
        )
    if raw.get("kind") == "List" and isinstance(raw.get("items"), list):
        items = raw["items"]
        if any(not isinstance(item, dict) for item in items):
            raise KubeconformSchemaConfigurationError(
                f"{scope.key}: Kubernetes List in {path} document {document_index + 1} "
                "contains a non-mapping item"
            )
        return tuple(items)
    return (raw,)


def _is_crd(gvk: GroupVersionKind) -> bool:
    return (
        gvk.group == "apiextensions.k8s.io"
        and gvk.version == "v1"
        and gvk.kind == "CustomResourceDefinition"
    )


__all__ = [
    "RenderedResource",
    "scan_rendered_directory",
]
