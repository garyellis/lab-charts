"""Release models shared by local resources."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from chart_manager.api.v1alpha1.common import StrictApiModel
from chart_manager.plumbing.names import dns_label
from chart_manager.plumbing.paths import relative_path

__all__ = [
    "BootstrapLifecycleRelease",
    "BootstrapLocalChartRelease",
    "BootstrapOciChartRelease",
    "BootstrapReadiness",
    "BootstrapRelease",
    "BootstrapRepoChartRelease",
    "LifecycleRelease",
    "LocalChartRelease",
    "OciChartRelease",
    "RepoChartRelease",
    "StackRelease",
    "WorkloadsReady",
]

_EXACT_SEMVER = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_HELM_DURATION = re.compile(r"^(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h))+$")
_HELM_DURATION_NUMBER = re.compile(r"(\d+(?:\.\d+)?)(?:ns|us|µs|ms|s|m|h)")
_KIND_RUNTIME_PLACEHOLDERS = frozenset(
    {
        "${kind.clusterName}",
        "${kind.context}",
        "${kind.controlPlaneHost}",
        "${kind.controlPlanePort}",
    }
)


def _paths(value: object, *, field: str) -> list[Path]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of repository-relative paths")
    return [relative_path(item, field=f"{field}[]") for item in value]


class LifecycleRelease(StrictApiModel):
    """Install a local chart through one authored lifecycle profile."""

    type: Literal["lifecycle"]
    chart: Path
    profile: str

    @field_validator("chart", mode="before")
    @classmethod
    def _safe_chart(cls, value: object) -> Path:
        return relative_path(value, field="release.chart")

    @field_validator("profile")
    @classmethod
    def _valid_profile(cls, value: str) -> str:
        return dns_label(value, field="release.profile")


class _RawHelmRelease(StrictApiModel):
    """Helm-owned settings that must be explicit for non-lifecycle releases."""

    name: str
    namespace: str
    values: list[Path]
    timeout: str

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return dns_label(value, field="release.name")

    @field_validator("namespace")
    @classmethod
    def _valid_namespace(cls, value: str) -> str:
        return dns_label(value, field="release.namespace")

    @field_validator("values", mode="before")
    @classmethod
    def _safe_values(cls, value: object) -> list[Path]:
        return _paths(value, field="release.values")

    @field_validator("timeout")
    @classmethod
    def _valid_timeout(cls, value: str) -> str:
        if value != value.strip() or not _HELM_DURATION.fullmatch(value):
            raise ValueError("release.timeout must be a positive Helm duration such as '10m'")
        if not any(Decimal(number) > 0 for number in _HELM_DURATION_NUMBER.findall(value)):
            raise ValueError("release.timeout must be greater than zero")
        return value


class LocalChartRelease(_RawHelmRelease):
    """Install a local chart directly, outside chart lifecycle ownership."""

    type: Literal["local"]
    chart: Path

    @field_validator("chart", mode="before")
    @classmethod
    def _safe_chart(cls, value: object) -> Path:
        return relative_path(value, field="release.chart")


class OciChartRelease(_RawHelmRelease):
    """Install an OCI chart pinned by one exact version or content digest."""

    type: Literal["oci"]
    chart: str
    version: str | None = None
    digest: str | None = None

    @field_validator("chart")
    @classmethod
    def _valid_chart(cls, value: str) -> str:
        if value != value.strip() or not value.startswith("oci://") or value == "oci://":
            raise ValueError("release.chart must be a non-empty oci:// reference")
        if "@" in value:
            raise ValueError("put the OCI digest in release.digest, not release.chart")
        return value

    @field_validator("version")
    @classmethod
    def _exact_version(cls, value: str | None) -> str | None:
        if value is not None and not _EXACT_SEMVER.fullmatch(value):
            raise ValueError("release.version must be an exact SemVer version")
        return value

    @field_validator("digest")
    @classmethod
    def _exact_digest(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError(
                "release.digest must be sha256 followed by 64 lowercase hexadecimal digits"
            )
        return value

    @model_validator(mode="after")
    def _exactly_one_pin(self) -> OciChartRelease:
        if (self.version is None) == (self.digest is None):
            raise ValueError("OCI release requires exactly one of version or digest")
        return self


class RepoChartRelease(_RawHelmRelease):
    """Install an exactly versioned chart from one HTTPS Helm repository."""

    type: Literal["repo"]
    repo: str
    chart: str
    version: str

    @field_validator("repo")
    @classmethod
    def _https_repo(cls, value: str) -> str:
        if value != value.strip() or not value.startswith("https://"):
            raise ValueError("release.repo must be an HTTPS URL beginning with https://")
        authority = value.removeprefix("https://").split("/", 1)[0]
        if (
            not authority
            or authority.startswith(".")
            or any(character in authority for character in "?#@")
        ):
            raise ValueError("release.repo must be an HTTPS URL with a host")
        return value

    @field_validator("chart")
    @classmethod
    def _bare_chart(cls, value: str) -> str:
        try:
            return dns_label(value, field="release.chart")
        except ValueError as exc:
            raise ValueError("release.chart must be a bare chart name") from exc

    @field_validator("version")
    @classmethod
    def _exact_version(cls, value: str) -> str:
        if not _EXACT_SEMVER.fullmatch(value):
            raise ValueError("release.version must be an exact SemVer version")
        return value


class WorkloadsReady(StrictApiModel):
    """Wait for every workload in one namespace after bootstrap installation."""

    namespace: str
    timeout: str

    @field_validator("namespace")
    @classmethod
    def _valid_namespace(cls, value: str) -> str:
        return dns_label(value, field="release.readiness.workloadsReady.namespace")

    @field_validator("timeout")
    @classmethod
    def _valid_timeout(cls, value: str) -> str:
        return _RawHelmRelease._valid_timeout(value)


class BootstrapReadiness(StrictApiModel):
    """Generic readiness gates applied after one bootstrap release."""

    nodes_ready: bool = Field(default=False, alias="nodesReady")
    workloads_ready: WorkloadsReady | None = Field(default=None, alias="workloadsReady")


class _BootstrapRelease:
    """Fields available only while bootstrapping a ``LocalCluster``.

    A plain class, not a model: Pydantic collects fields and validators from
    every entry in the MRO, so this stays a mixin that contributes no
    ``model_config`` of its own. Listing it first preserves field order.
    """

    runtime_values: dict[str, str] = Field(default_factory=dict, alias="runtimeValues")
    readiness: BootstrapReadiness | None = None

    @field_validator("runtime_values")
    @classmethod
    def _known_runtime_values(cls, values: dict[str, str]) -> dict[str, str]:
        invalid = sorted(set(values.values()) - _KIND_RUNTIME_PLACEHOLDERS)
        if invalid:
            supported = ", ".join(sorted(_KIND_RUNTIME_PLACEHOLDERS))
            raise ValueError(
                "release.runtimeValues values must be Kind runtime placeholders; "
                f"unsupported: {', '.join(invalid)}; supported: {supported}"
            )
        return values


class BootstrapLifecycleRelease(_BootstrapRelease, LifecycleRelease):
    """Lifecycle release augmented with bootstrap runtime contracts."""


class BootstrapLocalChartRelease(_BootstrapRelease, LocalChartRelease):
    """Raw local release augmented with bootstrap runtime contracts."""


class BootstrapOciChartRelease(_BootstrapRelease, OciChartRelease):
    """OCI release augmented with bootstrap runtime contracts."""


class BootstrapRepoChartRelease(_BootstrapRelease, RepoChartRelease):
    """HTTPS repository release augmented with bootstrap runtime contracts."""


type BootstrapRelease = Annotated[
    BootstrapLifecycleRelease
    | BootstrapLocalChartRelease
    | BootstrapOciChartRelease
    | BootstrapRepoChartRelease,
    Field(discriminator="type"),
]
type StackRelease = Annotated[
    LifecycleRelease | OciChartRelease | RepoChartRelease,
    Field(discriminator="type"),
]
