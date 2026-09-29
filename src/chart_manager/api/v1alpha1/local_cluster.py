"""The authored ``LocalCluster`` contract."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, get_args

from pydantic import Field, field_validator

from chart_manager.api.v1alpha1.common import ApiVersion, StrictApiModel
from chart_manager.api.v1alpha1.releases import BootstrapRelease, ResourceMetadata
from chart_manager.plumbing.paths import relative_path

LocalClusterKind = Literal["LocalCluster"]
LOCAL_CLUSTER_KIND: LocalClusterKind = get_args(LocalClusterKind)[0]


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

    api_version: ApiVersion = Field(alias="apiVersion")
    kind: LocalClusterKind
    metadata: ResourceMetadata
    spec: LocalClusterSpec
