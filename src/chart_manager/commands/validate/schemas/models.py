"""Stable value objects shared by CRD generation, locking, and synchronization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaConfigurationError,
)

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

    @property
    def key(self) -> str:
        return f"{self.chart}/{self.environment or '*'}"


class RepositoryPin(_StrictModel):
    repository: str
    track: str
    resolved: str

    @field_validator("repository")
    @classmethod
    def _repository(cls, value: str) -> str:
        parts = value.split("/")
        if len(parts) != 2 or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) for part in parts
        ):
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


class SchemaLock(_StrictModel):
    """Only upstream policy and repository pins belong in the committed lock."""

    version: Literal[2] = 2
    workspace: str
    generation: str
    policy: LockedSchemaPolicy

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            raise ValueError("workspace must be a non-empty path-safe name")
        return value

    @model_validator(mode="after")
    def _check_generation(self) -> SchemaLock:
        expected = generation_digest(workspace=self.workspace, policy=self.policy)
        if self.generation != expected:
            raise ValueError(f"generation digest mismatch: expected {expected}")
        return self


@dataclass(frozen=True)
class MaterializedSchema:
    """Schema bytes produced locally before an immutable generation exists."""

    gvk: GroupVersionKind
    content: bytes
    source_reference: str

    def __post_init__(self) -> None:
        if not self.content:
            raise KubeconformSchemaConfigurationError(
                "materialized schema content must not be empty"
            )


def content_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def generation_digest(*, workspace: str, policy: LockedSchemaPolicy) -> str:
    payload = {
        "version": 2,
        "workspace": workspace,
        "policy": policy.model_dump(mode="json", by_alias=True),
    }
    return content_digest(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def build_lock(*, workspace: str, policy: LockedSchemaPolicy) -> SchemaLock:
    return SchemaLock(
        workspace=workspace,
        policy=policy,
        generation=generation_digest(workspace=workspace, policy=policy),
    )


__all__ = [
    "GroupVersionKind",
    "LockedSchemaPolicy",
    "MaterializedSchema",
    "RepositoryPin",
    "SchemaLock",
    "SchemaScope",
    "build_lock",
    "content_digest",
]
