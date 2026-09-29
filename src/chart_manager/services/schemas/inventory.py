"""Inventory Kubernetes GVKs and CRDs from explicitly scoped rendered trees."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chart_manager.plumbing.yaml_files import load_yaml_documents
from chart_manager.services.schemas.errors import SchemaIntegrityError
from chart_manager.services.schemas.models import (
    GroupVersionKind,
    SchemaRequirement,
    SchemaScope,
    sort_requirements,
)

# JSON is a YAML subset and Helm may preserve JSON-formatted CRDs in the
# rendered tree. Inventory every manifest format accepted by kubeconform so
# the lock cannot omit resources that validation will later see.
_MANIFEST_SUFFIXES = frozenset({".json", ".yaml", ".yml"})


@dataclass(frozen=True)
class RenderedResource:
    gvk: GroupVersionKind
    scope: SchemaScope
    document: dict[str, Any]
    path: Path
    document_index: int


@dataclass(frozen=True)
class SchemaInventory:
    requirements: tuple[SchemaRequirement, ...]
    crds: tuple[RenderedResource, ...]
    resources: tuple[RenderedResource, ...]

    @classmethod
    def merge(cls, *inventories: SchemaInventory) -> SchemaInventory:
        resources = tuple(
            sorted(
                (resource for inventory in inventories for resource in inventory.resources),
                key=_resource_sort_key,
            )
        )
        crds = tuple(resource for resource in resources if _is_crd(resource.gvk))
        return cls(
            requirements=sort_requirements(
                [requirement for inventory in inventories for requirement in inventory.requirements]
            ),
            crds=crds,
            resources=resources,
        )


def scan_rendered_directory(
    directory: Path,
    *,
    scope: SchemaScope,
    allow_missing: frozenset[str] = frozenset(),
) -> SchemaInventory:
    """Read all YAML documents under one rendered chart/environment directory."""
    root = directory.resolve()
    if not root.is_dir():
        raise SchemaIntegrityError(f"rendered directory does not exist: {root}")
    resources: list[RenderedResource] = []
    for path in sorted(
        candidate
        for candidate in root.rglob("*")
        if candidate.is_file() and candidate.suffix.lower() in _MANIFEST_SUFFIXES
    ):
        try:
            documents = load_yaml_documents(path)
        except Exception as exc:
            raise SchemaIntegrityError(f"failed to inventory rendered YAML {path}: {exc}") from exc
        for index, raw in enumerate(documents):
            for document in _resource_documents(raw, path=path, document_index=index):
                try:
                    gvk = GroupVersionKind.from_document(document)
                except (TypeError, ValueError) as exc:
                    raise SchemaIntegrityError(
                        f"invalid Kubernetes resource in {path} document {index + 1}: {exc}"
                    ) from exc
                resources.append(
                    RenderedResource(
                        gvk=gvk,
                        scope=scope,
                        document=document,
                        # Provenance is committed into the lock through generated
                        # CRD schemas, so it must not depend on the machine's
                        # absolute render directory.
                        path=path.relative_to(root),
                        document_index=index,
                    )
                )
    ordered = tuple(sorted(resources, key=_resource_sort_key))
    requirements = sort_requirements(
        [
            SchemaRequirement(
                gvk=resource.gvk,
                scope=scope,
                allow_missing=(
                    resource.gvk.kind in allow_missing or resource.gvk.key in allow_missing
                    # yannh/kubernetes-json-schema publishes CRD component
                    # definitions but no top-level CustomResourceDefinition
                    # schema. Keep the exception explicit in the lock so the
                    # validator skip is derived from verified inventory rather
                    # than hidden in the kubeconform adapter.
                    or _is_crd(resource.gvk)
                ),
            )
            for resource in ordered
        ]
    )
    return SchemaInventory(
        requirements=requirements,
        crds=tuple(resource for resource in ordered if _is_crd(resource.gvk)),
        resources=ordered,
    )


def _resource_documents(
    raw: Any,
    *,
    path: Path,
    document_index: int,
) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise SchemaIntegrityError(
            f"rendered YAML {path} document {document_index + 1} must be a mapping"
        )
    if raw.get("kind") == "List" and isinstance(raw.get("items"), list):
        items = raw["items"]
        if any(not isinstance(item, dict) for item in items):
            raise SchemaIntegrityError(
                f"Kubernetes List in {path} document {document_index + 1} "
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


def _resource_sort_key(value: RenderedResource) -> tuple[str, str, str, str, str, str, int]:
    return (
        value.scope.chart,
        value.scope.environment or "",
        value.gvk.group,
        value.gvk.version,
        value.gvk.kind,
        value.path.as_posix(),
        value.document_index,
    )


__all__ = [
    "RenderedResource",
    "SchemaInventory",
    "scan_rendered_directory",
]
