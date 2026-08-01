"""``local.cmg.io/v1alpha1`` -- the authored ``LocalCluster``/``LocalStack`` contract.

``LocalCluster`` owns the Kind configuration and the ordered bootstrap
sequence.  ``LocalStack`` is a reusable application composition.  This module
owns the complete accepted shape of both: the envelopes, the three release
variants and the two discriminated unions over them, the bootstrap-only
additions, and every rule that can be decided by reading one document.

Paths here are validated lexically only -- a relative spelling with no empty,
``.`` or ``..`` segments.  Resolving them against the repository root,
checking that the file exists and agreeing a release name with ``Chart.yaml``
all need more than the document and therefore live in
``chart_manager.services.local_resources``; looking up a lifecycle profile
lives in ``chart_manager.services.domain.cluster_test_policy``.

Declaration order is load-bearing and matches the original module: the
``_BootstrapRelease`` mixin must precede the three ``Bootstrap*`` classes that
list it first among their bases, and both union aliases must follow all of
their members.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from chart_manager.api.base import StrictApiModel
from chart_manager.plumbing.names import dns_label
from chart_manager.plumbing.paths import relative_path

__all__ = [
    "LOCAL_API_VERSION",
    "LOCAL_CLUSTER_KIND",
    "LOCAL_STACK_KIND",
    "BootstrapLifecycleRelease",
    "BootstrapLocalChartRelease",
    "BootstrapOciChartRelease",
    "BootstrapReadiness",
    "BootstrapRelease",
    "LifecycleRelease",
    "LocalBootstrap",
    "LocalChartRelease",
    "LocalCluster",
    "LocalClusterSettings",
    "LocalClusterSpec",
    "LocalStack",
    "LocalStackSpec",
    "OciChartRelease",
    "ResourceMetadata",
    "StackRelease",
    "WorkloadsReady",
]

LOCAL_API_VERSION = "local.cmg.io/v1alpha1"
LOCAL_CLUSTER_KIND = "LocalCluster"
LOCAL_STACK_KIND = "LocalStack"

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


class ResourceMetadata(StrictApiModel):
    name: str

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return dns_label(value, field="metadata.name")


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
    workloads_ready: WorkloadsReady | None = Field(
        default=None,
        alias="workloadsReady",
    )


class _BootstrapRelease:
    """Fields available only while bootstrapping a ``LocalCluster``.

    A plain class, not a model: Pydantic collects fields and validators from
    every entry in the MRO, so this stays a mixin that contributes no
    ``model_config`` of its own.  Listing it first among each subclass's bases
    is what puts ``runtimeValues`` and ``readiness`` ahead of the release's own
    fields in the collected field order.
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


type BootstrapRelease = Annotated[
    BootstrapLifecycleRelease | BootstrapLocalChartRelease | BootstrapOciChartRelease,
    Field(discriminator="type"),
]
# Deliberately narrower than BootstrapRelease: a `type: local` release is
# accepted while bootstrapping a cluster but rejected inside a stack.
type StackRelease = Annotated[
    LifecycleRelease | OciChartRelease,
    Field(discriminator="type"),
]


class LocalClusterSettings(StrictApiModel):
    config: Path

    @field_validator("config", mode="before")
    @classmethod
    def _safe_config(cls, value: object) -> Path:
        return relative_path(value, field="spec.cluster.config")


class LocalBootstrap(StrictApiModel):
    releases: list[BootstrapRelease] = Field(default_factory=list)


class LocalClusterSpec(StrictApiModel):
    cluster: LocalClusterSettings
    bootstrap: LocalBootstrap


class LocalCluster(StrictApiModel):
    api_version: Literal["local.cmg.io/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["LocalCluster"]
    metadata: ResourceMetadata
    spec: LocalClusterSpec


class LocalStackSpec(StrictApiModel):
    releases: list[StackRelease] = Field(min_length=1)


class LocalStack(StrictApiModel):
    api_version: Literal["local.cmg.io/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["LocalStack"]
    metadata: ResourceMetadata
    spec: LocalStackSpec
