"""The authored ``ChartWorkspace`` repository-policy contract."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, get_args

from pydantic import ConfigDict, Field, field_validator

from chart_manager.api.v1alpha1.common import ApiVersion, StrictApiModel
from chart_manager.plumbing.names import dns_label
from chart_manager.plumbing.paths import relative_path

ChartWorkspaceKind = Literal["ChartWorkspace"]
CHART_WORKSPACE_KIND: ChartWorkspaceKind = get_args(ChartWorkspaceKind)[0]


def _workspace_path(value: object, *, field: str, allow_dot: bool = False) -> Path:
    if allow_dot and value in {".", Path(".")}:
        return Path(".")
    return relative_path(value, field=field)


def _fanout_pattern(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a repository-relative POSIX pattern")
    raw = value[2:] if value.startswith("./") else value
    if (
        not raw
        or "\\" in raw
        or raw.startswith("/")
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise ValueError(
            f"{field} must be a repository-relative POSIX pattern without "
            "empty, '.' or '..' segments"
        )
    first = raw.split("/", 1)[0]
    if ":" in first:
        raise ValueError(f"{field} must not be drive-qualified")
    if any("**" in part and part != "**" for part in raw.split("/")):
        raise ValueError(f"{field} uses '**' only as a complete path segment")
    if any(character in raw for character in "?[]"):
        raise ValueError(f"{field} supports only '*' and '**' wildcard syntax")
    return raw


class WorkspaceFanout(StrictApiModel):
    """Additional repository inputs that invalidate a complete planner."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    validation: tuple[str, ...] = ()
    cluster_test: tuple[str, ...] = Field(default=(), alias="clusterTest")

    @field_validator("validation", "cluster_test", mode="before")
    @classmethod
    def _patterns(cls, value: object, info) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"spec.fanout.{info.field_name} must be a list")
        normalized = [
            _fanout_pattern(item, field=f"spec.fanout.{info.field_name}[]") for item in value
        ]
        return tuple(sorted(set(normalized)))


class WorkspaceClusterTest(StrictApiModel):
    """Repository-wide cluster-test policy."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    shared_prerequisites: tuple[str, ...] = Field(
        default=(),
        alias="sharedPrerequisites",
    )

    @field_validator("shared_prerequisites", mode="before")
    @classmethod
    def _chart_names(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("spec.clusterTest.sharedPrerequisites must be a list")
        return tuple(
            sorted(
                {
                    dns_label(name, field="spec.clusterTest.sharedPrerequisites[]")
                    for name in value
                }
            )
        )


class ChartWorkspaceSpec(StrictApiModel):
    """Checkout-owned layout, dependency, and planning policy."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    charts_dir: Path = Field(alias="chartsDir")
    local_cluster: Path = Field(alias="localCluster")
    render_dir: Path = Field(alias="renderDir")
    policies_dir: Path = Field(alias="policiesDir")
    fanout: WorkspaceFanout = Field(default_factory=WorkspaceFanout)
    cluster_test: WorkspaceClusterTest = Field(
        default_factory=WorkspaceClusterTest,
        alias="clusterTest",
    )

    @field_validator("charts_dir", mode="before")
    @classmethod
    def _charts_path(cls, value: object) -> Path:
        return _workspace_path(value, field="spec.chartsDir", allow_dot=True)

    @field_validator("local_cluster", "render_dir", "policies_dir", mode="before")
    @classmethod
    def _repository_path(cls, value: object, info) -> Path:
        return _workspace_path(value, field=f"spec.{info.field_name}")


class WorkspaceMetadata(StrictApiModel):
    """Frozen DNS-label identity for a workspace."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return dns_label(value, field="metadata.name")


class ChartWorkspace(StrictApiModel):
    """Frozen authored envelope at ``.chart-manager/workspace.yaml``."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    api_version: ApiVersion = Field(alias="apiVersion")
    kind: ChartWorkspaceKind
    metadata: WorkspaceMetadata
    spec: ChartWorkspaceSpec


__all__ = [
    "CHART_WORKSPACE_KIND",
    "ChartWorkspace",
    "ChartWorkspaceKind",
    "ChartWorkspaceSpec",
    "WorkspaceClusterTest",
    "WorkspaceFanout",
    "WorkspaceMetadata",
]
