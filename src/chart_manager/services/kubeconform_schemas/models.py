"""Stable value objects shared by schema inventory, locking, and synchronization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chart_manager.services.kubeconform_schemas.errors import (
    KubeconformSchemaConfigurationError,
)

SchemaSourceKind = Literal["generated", "local", "kubernetes", "catalog"]
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)


class GroupVersionKind(_StrictModel):
    """A Kubernetes resource identity; an empty group denotes the core API."""

    group: str = ""
    version: str
    kind: str

    @field_validator("group", "version", "kind")
    @classmethod
    def _clean_component(cls, value: str, info) -> str:
        if value != value.strip() or "/" in value or "\\" in value:
            raise ValueError(f"{info.field_name} must be a trimmed path-safe value")
        if info.field_name != "group" and not value:
            raise ValueError(f"{info.field_name} must not be empty")
        patterns = {
            "group": r"(?:[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?)?",
            "version": r"[A-Za-z0-9][A-Za-z0-9.-]*",
            "kind": r"[A-Za-z][A-Za-z0-9]*",
        }
        if not re.fullmatch(patterns[info.field_name], value):
            raise ValueError(f"{info.field_name} is not a valid Kubernetes identity component")
        return value

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> GroupVersionKind:
        api_version = document.get("apiVersion")
        kind = document.get("kind")
        if not isinstance(api_version, str) or not isinstance(kind, str):
            raise ValueError("Kubernetes resource needs string apiVersion and kind")
        if "/" in api_version:
            group, version = api_version.split("/", 1)
        else:
            group, version = "", api_version
        return cls(group=group, version=version, kind=kind)

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}" if self.group else self.version

    @property
    def key(self) -> str:
        return f"{self.api_version}/{self.kind}"


class SchemaScope(_StrictModel):
    """The chart, and optionally environment, whose CRD/schema owns an entry."""

    chart: str
    environment: str | None = None

    @field_validator("chart", "environment")
    @classmethod
    def _clean(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        if not value or value != value.strip() or any(c in value for c in "/\\"):
            raise ValueError(f"{info.field_name} must be a non-empty path-safe value")
        return value

    def covers(self, required: SchemaScope) -> bool:
        return self.chart == required.chart and (
            self.environment is None or self.environment == required.environment
        )

    @property
    def key(self) -> str:
        return f"{self.chart}/{self.environment or '*'}"


class SchemaRequirement(_StrictModel):
    """One GVK required by one rendered chart/environment."""

    gvk: GroupVersionKind
    scope: SchemaScope
    allow_missing: bool = Field(default=False, alias="allowMissing")


class RepositoryPin(_StrictModel):
    repository: str
    track: str
    resolved: str

    @field_validator("repository")
    @classmethod
    def _repository(cls, value: str) -> str:
        parts = value.split("/")
        if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
            raise ValueError("repository must be an owner/name GitHub repository")
        return value

    @field_validator("track")
    @classmethod
    def _track(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("track must not be empty or padded")
        return value

    @field_validator("resolved")
    @classmethod
    def _resolved(cls, value: str) -> str:
        if not _FULL_SHA_RE.fullmatch(value):
            raise ValueError("resolved must be a lowercase full Git commit SHA")
        return value


class LockedSchemaPolicy(_StrictModel):
    kubernetes_version: str = Field(alias="kubernetesVersion")
    generate_from_crds: bool = Field(alias="generateFromCRDs")
    kubernetes: RepositoryPin
    catalog: RepositoryPin

    @field_validator("kubernetes_version")
    @classmethod
    def _version(cls, value: str) -> str:
        normalized = value.removeprefix("v")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", normalized):
            raise ValueError("kubernetesVersion must be an exact Kubernetes version")
        return normalized


class SchemaFile(_StrictModel):
    """One immutable schema object in a store generation."""

    gvk: GroupVersionKind
    source: SchemaSourceKind
    path: str
    sha256: str
    scope: SchemaScope | None = None
    source_reference: str = Field(alias="sourceReference")

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("path must be a safe relative POSIX path")
        return path.as_posix()

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("sha256 must be sha256:<64 lowercase hex characters>")
        return value

    @model_validator(mode="after")
    def _scope_matches_source(self) -> SchemaFile:
        if self.source in {"generated", "local"} and self.scope is None:
            raise ValueError(f"{self.source} schema entries require a scope")
        if self.source in {"kubernetes", "catalog"} and self.scope is not None:
            raise ValueError(f"{self.source} schema entries must be shared")
        return self


class SchemaLock(_StrictModel):
    """The committed, generated contract for one complete schema generation."""

    version: Literal[1] = 1
    workspace: str
    generation: str
    policy: LockedSchemaPolicy
    inventory: tuple[SchemaRequirement, ...]
    schemas: tuple[SchemaFile, ...]

    @field_validator("inventory", "schemas", mode="before")
    @classmethod
    def _yaml_sequences(cls, value: Any) -> Any:
        # YAML has no tuple representation. Preserve strict validation for the
        # contained models while accepting the sequence emitted by our stable
        # serializer on a subsequent load.
        return tuple(value) if isinstance(value, list) else value

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, value: str) -> str:
        if not value or value != value.strip() or any(c in value for c in "/\\"):
            raise ValueError("workspace must be a non-empty path-safe name")
        return value

    @field_validator("generation")
    @classmethod
    def _generation(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("generation must be a sha256 digest")
        return value

    @model_validator(mode="after")
    def _canonical_and_complete(self) -> SchemaLock:
        if self.inventory != sort_requirements(self.inventory):
            raise ValueError("inventory must be sorted and deduplicated")
        if self.schemas != sort_schema_files(self.schemas):
            raise ValueError("schemas must be sorted and deduplicated")
        expected = generation_digest(
            version=self.version,
            workspace=self.workspace,
            policy=self.policy,
            inventory=self.inventory,
            schemas=self.schemas,
        )
        if self.generation != expected:
            raise ValueError(f"generation digest mismatch: expected {expected}")
        return self


@dataclass(frozen=True)
class MaterializedSchema:
    """Schema bytes produced locally before an immutable generation exists."""

    gvk: GroupVersionKind
    source: Literal["generated", "local"]
    scope: SchemaScope
    content: bytes
    source_reference: str

    def __post_init__(self) -> None:
        if not self.content:
            raise KubeconformSchemaConfigurationError(
                "materialized schema content must not be empty"
            )

    @property
    def sha256(self) -> str:
        return content_digest(self.content)


@dataclass(frozen=True)
class AuthoredSchemaPolicy:
    kubernetes_version: str
    generate_from_crds: bool
    catalog_repository: str
    catalog_track: str
    kubernetes_repository: str = "yannh/kubernetes-json-schema"
    kubernetes_track: str = "master"

    def normalized_version(self) -> str:
        return self.kubernetes_version.removeprefix("v")


def content_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def requirement_sort_key(value: SchemaRequirement) -> tuple[str, str, str, str, str, bool]:
    return (
        value.gvk.group,
        value.gvk.version,
        value.gvk.kind,
        value.scope.chart,
        value.scope.environment or "",
        value.allow_missing,
    )


def schema_file_sort_key(value: SchemaFile) -> tuple[str, str, str, str, str, str, str]:
    return (
        value.gvk.group,
        value.gvk.version,
        value.gvk.kind,
        value.scope.chart if value.scope else "",
        value.scope.environment or "" if value.scope else "",
        value.source,
        value.path,
    )


def sort_requirements(
    values: tuple[SchemaRequirement, ...] | list[SchemaRequirement],
) -> tuple[SchemaRequirement, ...]:
    keyed: dict[tuple[str, str, str, str, str], SchemaRequirement] = {}
    for value in values:
        key = (
            value.gvk.group,
            value.gvk.version,
            value.gvk.kind,
            value.scope.chart,
            value.scope.environment or "",
        )
        previous = keyed.get(key)
        if previous is not None and previous.allow_missing != value.allow_missing:
            raise KubeconformSchemaConfigurationError(
                f"conflicting missing-schema policy for {value.gvk.key} in {value.scope.key}"
            )
        keyed[key] = value
    return tuple(sorted(keyed.values(), key=requirement_sort_key))


def sort_schema_files(values: tuple[SchemaFile, ...] | list[SchemaFile]) -> tuple[SchemaFile, ...]:
    keyed: dict[tuple[str, str, str, str, str, str], SchemaFile] = {}
    for value in values:
        key = (
            value.gvk.group,
            value.gvk.version,
            value.gvk.kind,
            value.scope.chart if value.scope else "",
            value.scope.environment or "" if value.scope else "",
            value.source,
        )
        previous = keyed.get(key)
        if previous is not None and previous != value:
            raise KubeconformSchemaConfigurationError(
                f"conflicting {value.source} schemas for {value.gvk.key}"
            )
        keyed[key] = value
    return tuple(sorted(keyed.values(), key=schema_file_sort_key))


def generation_digest(
    *,
    version: int,
    workspace: str,
    policy: LockedSchemaPolicy,
    inventory: tuple[SchemaRequirement, ...],
    schemas: tuple[SchemaFile, ...],
) -> str:
    payload = {
        "version": version,
        "workspace": workspace,
        "policy": policy.model_dump(mode="json", by_alias=True),
        "inventory": [item.model_dump(mode="json", by_alias=True) for item in inventory],
        "schemas": [item.model_dump(mode="json", by_alias=True) for item in schemas],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return content_digest(encoded)


def build_lock(
    *,
    workspace: str,
    policy: LockedSchemaPolicy,
    inventory: tuple[SchemaRequirement, ...] | list[SchemaRequirement],
    schemas: tuple[SchemaFile, ...] | list[SchemaFile],
) -> SchemaLock:
    ordered_inventory = sort_requirements(inventory)
    ordered_schemas = sort_schema_files(schemas)
    digest = generation_digest(
        version=1,
        workspace=workspace,
        policy=policy,
        inventory=ordered_inventory,
        schemas=ordered_schemas,
    )
    return SchemaLock(
        version=1,
        workspace=workspace,
        generation=digest,
        policy=policy,
        inventory=ordered_inventory,
        schemas=ordered_schemas,
    )


__all__ = [
    "AuthoredSchemaPolicy",
    "GroupVersionKind",
    "LockedSchemaPolicy",
    "MaterializedSchema",
    "RepositoryPin",
    "SchemaFile",
    "SchemaLock",
    "SchemaRequirement",
    "SchemaScope",
    "SchemaSourceKind",
    "build_lock",
    "content_digest",
    "sort_requirements",
    "sort_schema_files",
]
