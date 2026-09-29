"""Convert rendered Kubernetes v1 CRDs into strict kubeconform JSON schemas."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from typing import Any

from chart_manager.services.schemas.errors import SchemaIntegrityError
from chart_manager.services.schemas.inventory import RenderedResource
from chart_manager.services.schemas.models import (
    GroupVersionKind,
    MaterializedSchema,
)

_SCHEMA_DRAFT = "http://json-schema.org/draft-07/schema#"


def generate_crd_schemas(
    crds: Iterable[RenderedResource],
) -> tuple[MaterializedSchema, ...]:
    """Generate deterministic schemas for every served CRD version with a schema."""
    generated: dict[tuple[str, str, str, str, str], MaterializedSchema] = {}
    for resource in crds:
        for artifact in _schemas_for_crd(resource):
            key = (
                artifact.scope.chart,
                artifact.scope.environment or "",
                artifact.gvk.group,
                artifact.gvk.version,
                artifact.gvk.kind,
            )
            previous = generated.get(key)
            if previous is not None and previous.content != artifact.content:
                raise SchemaIntegrityError(
                    f"conflicting rendered CRDs define {artifact.gvk.key} "
                    f"in {artifact.scope.key}: {previous.source_reference}, "
                    f"{artifact.source_reference}"
                )
            generated[key] = artifact
    return tuple(
        sorted(
            generated.values(),
            key=lambda item: (
                item.scope.chart,
                item.scope.environment or "",
                item.gvk.group,
                item.gvk.version,
                item.gvk.kind,
            ),
        )
    )


def _schemas_for_crd(resource: RenderedResource) -> tuple[MaterializedSchema, ...]:
    document = resource.document
    spec = document.get("spec")
    if not isinstance(spec, dict):
        raise SchemaIntegrityError(f"CRD in {resource.path} has no mapping spec")
    group = spec.get("group")
    names = spec.get("names")
    versions = spec.get("versions")
    kind = names.get("kind") if isinstance(names, dict) else None
    if not isinstance(group, str) or not group or not isinstance(kind, str) or not kind:
        raise SchemaIntegrityError(f"CRD in {resource.path} has invalid spec.group/spec.names.kind")
    if not isinstance(versions, list) or not versions:
        raise SchemaIntegrityError(f"CRD {group}/{kind} in {resource.path} has no versions")

    artifacts: list[MaterializedSchema] = []
    for raw_version in versions:
        if not isinstance(raw_version, dict):
            raise SchemaIntegrityError(f"CRD {group}/{kind} has a non-mapping version")
        if raw_version.get("served") is not True:
            continue
        version = raw_version.get("name")
        schema_container = raw_version.get("schema")
        openapi = (
            schema_container.get("openAPIV3Schema")
            if isinstance(schema_container, dict)
            else None
        )
        # Non-structural/legacy CRDs remain eligible for the local/catalog fallback.
        if openapi is None:
            continue
        if not isinstance(version, str) or not version or not isinstance(openapi, dict):
            raise SchemaIntegrityError(f"CRD {group}/{kind} has an invalid served version schema")
        normalized = copy.deepcopy(openapi)
        properties = normalized.setdefault("properties", {})
        if not isinstance(properties, dict):
            raise SchemaIntegrityError(
                f"CRD {group}/{kind} version {version} has non-mapping properties"
            )
        # A CRD's OpenAPI schema describes the custom fields admitted by the API
        # server, while kubeconform validates the complete resource document.
        # Supply the standard resource envelope when the CRD did not spell it out.
        properties.setdefault(
            "apiVersion",
            {"type": "string", "enum": [f"{group}/{version}"]},
        )
        properties.setdefault("kind", {"type": "string", "enum": [kind]})
        properties.setdefault(
            "metadata",
            {"type": "object", "additionalProperties": True},
        )
        required = normalized.setdefault("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise SchemaIntegrityError(
                f"CRD {group}/{kind} version {version} has invalid required fields"
            )
        normalized["required"] = sorted({*required, "apiVersion", "kind", "metadata"})
        normalized = _strict_schema(normalized)
        normalized.setdefault("$schema", _SCHEMA_DRAFT)
        content = (json.dumps(normalized, indent=2, sort_keys=True) + "\n").encode()
        artifacts.append(
            MaterializedSchema(
                gvk=GroupVersionKind(group=group, version=version, kind=kind),
                source="generated",
                scope=resource.scope,
                content=content,
                source_reference=(
                    f"{resource.path.as_posix()}#document={resource.document_index + 1};"
                    f"version={version}"
                ),
            )
        )
    return tuple(artifacts)


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make closed object shapes explicit without changing maps or preserved fields."""
    _walk_schema(schema)
    return schema


def _walk_schema(node: dict[str, Any]) -> None:
    properties = node.get("properties")
    if isinstance(properties, dict):
        for child in properties.values():
            if isinstance(child, dict):
                _walk_schema(child)

    for key in ("patternProperties", "definitions", "$defs", "dependentSchemas"):
        children = node.get(key)
        if isinstance(children, dict):
            for child in children.values():
                if isinstance(child, dict):
                    _walk_schema(child)

    items = node.get("items")
    if isinstance(items, dict):
        _walk_schema(items)
    elif isinstance(items, list):
        for child in items:
            if isinstance(child, dict):
                _walk_schema(child)

    additional = node.get("additionalProperties")
    if isinstance(additional, dict):
        _walk_schema(additional)

    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        children = node.get(key)
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _walk_schema(child)
    for key in ("not", "if", "then", "else", "contains", "propertyNames"):
        child = node.get(key)
        if isinstance(child, dict):
            _walk_schema(child)

    object_shape = node.get("type") == "object" or isinstance(properties, dict)
    preserve_unknown = node.get("x-kubernetes-preserve-unknown-fields") is True
    if object_shape and "additionalProperties" not in node and not preserve_unknown:
        node["additionalProperties"] = False


__all__ = ["generate_crd_schemas"]
