"""``local.chartmanager.io/v1alpha1`` -- the authored ``LocalCluster``/``LocalStack`` contract.

``LocalCluster`` owns the Kind configuration and the ordered bootstrap
sequence.  ``LocalStack`` is a reusable application composition.  This module
owns the complete accepted shape of both: the envelopes, the four release
variants and the two discriminated unions over them, the bootstrap-only
additions, and every rule that can be decided by reading one document.

Paths here are validated lexically only -- a relative spelling with no empty,
``.`` or ``..`` segments.  Resolving them against the repository root,
checking that the file exists and agreeing a release name with ``Chart.yaml``
all need more than the document and therefore live in
``chart_manager.domain.local_resources``; looking up a lifecycle profile
lives in ``chart_manager.domain.lifecycle_policy``.

Declaration order is load-bearing and matches the original module: the
``_BootstrapRelease`` mixin must precede the four ``Bootstrap*`` classes that
list it first among their bases, and both union aliases must follow all of
their members.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, get_args

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
    "BootstrapRepoChartRelease",
    "LifecycleRelease",
    "LocalApiVersion",
    "LocalBootstrap",
    "LocalChartRelease",
    "LocalCluster",
    "LocalClusterKind",
    "LocalClusterSettings",
    "LocalClusterSpec",
    "LocalStack",
    "LocalStackKind",
    "LocalStackSpec",
    "OciChartRelease",
    "ProvisioningHooks",
    "RepoChartRelease",
    "ResourceMetadata",
    "StackRelease",
    "WorkloadsReady",
]

# The group string and both kinds are each spelled exactly once, here. The two
# envelopes annotate their fields with these aliases and the constants are read
# back out of them, so a rename cannot leave `LocalCluster` and `LocalStack`
# disagreeing about what they accept -- which is exactly what happened while
# the group moved off `cmg.io`.
#
# Plain assignment, not `type X = ...`: a PEP 695 alias makes Pydantic emit a
# `$ref` into `$defs` instead of an inline `const`, which would change the
# generated JSON Schema for no benefit.
LocalApiVersion = Literal["local.chartmanager.io/v1alpha1"]
LocalClusterKind = Literal["LocalCluster"]
LocalStackKind = Literal["LocalStack"]

LOCAL_API_VERSION: LocalApiVersion = get_args(LocalApiVersion)[0]
LOCAL_CLUSTER_KIND: LocalClusterKind = get_args(LocalClusterKind)[0]
LOCAL_STACK_KIND: LocalStackKind = get_args(LocalStackKind)[0]

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
    """Identity shared by both local kinds.

    `name` is a lowercase DNS label, which is stricter than
    `ChartLifecycleMetadata.name` in the lifecycle group -- that one accepts any
    non-padded string. The two are separate models on purpose; merging them
    would change what one of the groups accepts.
    """

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


class BootstrapRepoChartRelease(_BootstrapRelease, RepoChartRelease):
    """HTTPS repository release augmented with bootstrap runtime contracts."""


type BootstrapRelease = Annotated[
    BootstrapLifecycleRelease
    | BootstrapLocalChartRelease
    | BootstrapOciChartRelease
    | BootstrapRepoChartRelease,
    Field(discriminator="type"),
]
# Deliberately narrower than BootstrapRelease: a `type: local` release is
# accepted while bootstrapping a cluster but rejected inside a stack.
type StackRelease = Annotated[
    LifecycleRelease | OciChartRelease | RepoChartRelease,
    Field(discriminator="type"),
]


class LocalClusterSettings(StrictApiModel):
    """Where the Kind configuration lives, as a repository-relative path.

    Chart-manager never interprets that file -- Kind does. Changing a
    creation-time setting inside it therefore needs `local reset`, not
    `local up`.
    """

    config: Path
    hooks: ProvisioningHooks | None = None

    @field_validator("config", mode="before")
    @classmethod
    def _safe_config(cls, value: object) -> Path:
        return relative_path(value, field="spec.cluster.config")


class ProvisioningHooks(StrictApiModel):
    """Optional fail-fast argv commands around environment provisioning."""

    pre_provision: list[str] | None = Field(default=None, alias="preProvision")
    post_provision: list[str] | None = Field(default=None, alias="postProvision")

    @field_validator("pre_provision", "post_provision")
    @classmethod
    def _valid_argv(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (not value or any(not item for item in value)):
            raise ValueError("provisioning hook must be a non-empty argv of non-empty strings")
        return value


class LocalBootstrap(StrictApiModel):
    """Releases installed after the cluster exists, in declaration order.

    The executor is fail-fast and waits for each release's readiness gates
    before starting the next, so list order is the install order and a failure
    stops the rest. An empty list is valid -- it means a bare cluster.
    """

    releases: list[BootstrapRelease] = Field(default_factory=list)


class LocalClusterSpec(StrictApiModel):
    """What a local cluster is made of: a Kind config, then a bootstrap sequence."""

    cluster: LocalClusterSettings
    bootstrap: LocalBootstrap


class LocalCluster(StrictApiModel):
    """Envelope for one authored local environment.

    Read from `.chart-manager/local-cluster.yaml` unless
    `CHART_MANAGER_LOCAL_CONFIG` points elsewhere. One per environment.
    """

    api_version: LocalApiVersion = Field(alias="apiVersion")
    kind: LocalClusterKind
    metadata: ResourceMetadata
    spec: LocalClusterSpec


class LocalStackSpec(StrictApiModel):
    """Releases composing one stack, at least one.

    Typed as `StackRelease`, not `BootstrapRelease`: a `type: local` release is
    rejected here. A stack must stay reusable, so every release names either a
    lifecycle profile, a pinned OCI chart, or an exactly versioned chart from
    an HTTPS Helm repository.
    """

    releases: list[StackRelease] = Field(min_length=1)


class LocalStack(StrictApiModel):
    """Envelope for a reusable application composition.

    Resolved by name from the `stacks/` directory beside the LocalCluster file,
    or from an explicit path. Composition only -- no templating or ordering
    language, which is what keeps it narrower than Helmfile.
    """

    api_version: LocalApiVersion = Field(alias="apiVersion")
    kind: LocalStackKind
    metadata: ResourceMetadata
    spec: LocalStackSpec
