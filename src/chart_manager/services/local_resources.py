"""Strict authored resources for repository-local Kubernetes environments.

``LocalCluster`` owns the Kind configuration and the ordered bootstrap
sequence. ``LocalStack`` is a reusable application composition.  Both use
repository-relative paths so loading them never grants access outside the
repository root.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.yaml_files import load_yaml_file
from chart_manager.services.chart_config import load_chart_lifecycle
from chart_manager.settings import DEFAULT_CHARTS_DIR

LOCAL_API_VERSION = "local.cmg.io/v1alpha1"
LOCAL_CLUSTER_KIND = "LocalCluster"
LOCAL_STACK_KIND = "LocalStack"
DEFAULT_LOCAL_CONFIG = Path(".chart-manager/local-cluster.yaml")
DEFAULT_LOCAL_CLUSTER_FILE = Path("local-cluster.yaml")
DEFAULT_STACKS_DIR = Path("stacks")

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
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


def _name(value: str, *, field: str) -> str:
    if len(value) > 63 or not _DNS_LABEL.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase DNS label of at most 63 characters")
    return value


def _relative_path(value: object, *, field: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{field} must be a repository-relative path")
    raw = str(value)
    # Check the authored spelling before Path normalizes away "." segments.
    if (
        not raw
        or "\\" in raw
        or raw.startswith("/")
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise ValueError(
            f"{field} must be a repository-relative path without empty, '.' or '..' segments"
        )
    path = Path(raw)
    if path.is_absolute():
        raise ValueError(f"{field} must be a repository-relative path")
    return path


def _paths(value: object, *, field: str) -> list[Path]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of repository-relative paths")
    return [_relative_path(item, field=f"{field}[]") for item in value]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResourceMetadata(_StrictModel):
    name: str

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return _name(value, field="metadata.name")


class LifecycleRelease(_StrictModel):
    """Install a local chart through one authored lifecycle profile."""

    type: Literal["lifecycle"]
    chart: Path
    profile: str

    @field_validator("chart", mode="before")
    @classmethod
    def _safe_chart(cls, value: object) -> Path:
        return _relative_path(value, field="release.chart")

    @field_validator("profile")
    @classmethod
    def _valid_profile(cls, value: str) -> str:
        return _name(value, field="release.profile")


class _RawHelmRelease(_StrictModel):
    """Helm-owned settings that must be explicit for non-lifecycle releases."""

    name: str
    namespace: str
    values: list[Path]
    timeout: str

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return _name(value, field="release.name")

    @field_validator("namespace")
    @classmethod
    def _valid_namespace(cls, value: str) -> str:
        return _name(value, field="release.namespace")

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
        return _relative_path(value, field="release.chart")


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


class WorkloadsReady(_StrictModel):
    """Wait for every workload in one namespace after bootstrap installation."""

    namespace: str
    timeout: str

    @field_validator("namespace")
    @classmethod
    def _valid_namespace(cls, value: str) -> str:
        return _name(value, field="release.readiness.workloadsReady.namespace")

    @field_validator("timeout")
    @classmethod
    def _valid_timeout(cls, value: str) -> str:
        return _RawHelmRelease._valid_timeout(value)


class BootstrapReadiness(_StrictModel):
    """Generic readiness gates applied after one bootstrap release."""

    nodes_ready: bool = Field(default=False, alias="nodesReady")
    workloads_ready: WorkloadsReady | None = Field(
        default=None,
        alias="workloadsReady",
    )


class _BootstrapRelease:
    """Fields available only while bootstrapping a ``LocalCluster``."""

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
type StackRelease = Annotated[
    LifecycleRelease | OciChartRelease,
    Field(discriminator="type"),
]


class LocalClusterSettings(_StrictModel):
    config: Path

    @field_validator("config", mode="before")
    @classmethod
    def _safe_config(cls, value: object) -> Path:
        return _relative_path(value, field="spec.cluster.config")


class LocalBootstrap(_StrictModel):
    releases: list[BootstrapRelease] = Field(default_factory=list)


class LocalClusterSpec(_StrictModel):
    cluster: LocalClusterSettings
    bootstrap: LocalBootstrap


class LocalCluster(_StrictModel):
    api_version: Literal["local.cmg.io/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["LocalCluster"]
    metadata: ResourceMetadata
    spec: LocalClusterSpec


class LocalStackSpec(_StrictModel):
    releases: list[StackRelease] = Field(min_length=1)


class LocalStack(_StrictModel):
    api_version: Literal["local.cmg.io/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["LocalStack"]
    metadata: ResourceMetadata
    spec: LocalStackSpec


def _load_resource(path: Path, model: type[LocalCluster] | type[LocalStack]):
    if not path.is_file():
        raise SpecError(f"local resource file does not exist: {path}")
    try:
        document = load_yaml_file(path)
    except (SpecError, yaml.YAMLError) as exc:
        raise SpecError(f"invalid local resource {path}: {exc}") from exc
    try:
        return model.model_validate(document)
    except ValueError as exc:
        raise SpecError(f"invalid local resource {path}: {exc}") from exc


def load_local_cluster(path: Path) -> LocalCluster:
    """Strictly load one ``LocalCluster`` resource."""
    return _load_resource(path, LocalCluster)


def load_local_stack(path: Path) -> LocalStack:
    """Strictly load one ``LocalStack`` resource."""
    return _load_resource(path, LocalStack)


class ResolvedChartTarget(_StrictModel):
    """An explicit chart directory selected as a local target."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["chart"] = "chart"
    name: str
    path: Path


class ResolvedStackTarget(_StrictModel):
    """A loaded stack and its canonical authored source."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["stack"] = "stack"
    name: str
    path: Path
    stack: LocalStack


type ResolvedLocalTarget = ResolvedChartTarget | ResolvedStackTarget


class LocalResourceLoader:
    """Load local resources and enforce repository containment/existence."""

    def __init__(
        self,
        root: Path,
        *,
        local_config: Path = DEFAULT_LOCAL_CONFIG,
        stacks_dir: Path = DEFAULT_STACKS_DIR,
    ) -> None:
        self.root = root.resolve()
        self.local_config = _relative_path(local_config, field="local_config")
        self.stacks_dir = _relative_path(stacks_dir, field="stacks_dir")

    @property
    def cluster_path(self) -> Path:
        return self.root / self.local_config

    @property
    def stacks_path(self) -> Path:
        return self.root / self.local_config.parent / self.stacks_dir

    def load_cluster(self) -> LocalCluster:
        cluster = load_local_cluster(self.cluster_path)
        self._require_file(cluster.spec.cluster.config, field="spec.cluster.config")
        for release in cluster.spec.bootstrap.releases:
            self._validate_release(release)
        return cluster

    def load_stack(self, path: Path) -> LocalStack:
        absolute = self._inside_root(path)
        stack = load_local_stack(absolute)
        for release in stack.spec.releases:
            self._validate_release(release)
        return stack

    def _validate_release(self, release: BootstrapRelease | StackRelease) -> None:
        if isinstance(release, (LifecycleRelease, LocalChartRelease)):
            chart = self._require_directory(release.chart, field="release.chart")
            chart_yaml = chart / "Chart.yaml"
            if not chart_yaml.is_file():
                raise SpecError(f"release.chart has no Chart.yaml: {release.chart}")
            chart_document = load_yaml_file(chart_yaml)
            chart_name = chart_document.get("name")
            if not isinstance(chart_name, str):
                raise SpecError(f"{chart_yaml} must define a string name")
            if isinstance(release, LocalChartRelease) and release.name != chart_name:
                raise SpecError(
                    f"local release name {release.name!r} does not match "
                    f"{chart_yaml} name {chart_name!r}"
                )
            if isinstance(release, LifecycleRelease):
                lifecycle_path = chart / "chart-lifecycle.yaml"
                lifecycle = load_chart_lifecycle(lifecycle_path)
                if lifecycle.metadata.name != chart_name:
                    raise SpecError(
                        f"{lifecycle_path} metadata.name {lifecycle.metadata.name!r} "
                        f"does not match {chart_yaml} name {chart_name!r}"
                    )
                cluster_test = lifecycle.spec.cluster_test
                if not lifecycle.spec.enabled or cluster_test is None or not cluster_test.enabled:
                    raise SpecError(
                        f"lifecycle release chart {release.chart} has no enabled clusterTest"
                    )
                cluster_test.profile(release.profile)
        if isinstance(release, (LocalChartRelease, OciChartRelease)):
            for path in release.values:
                self._require_file(path, field="release.values[]")

    def _inside_root(self, path: Path) -> Path:
        candidate = path if path.is_absolute() else self.root / path
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SpecError(f"path escapes repository root {self.root}: {path}") from exc
        return resolved

    def _require_file(self, path: Path, *, field: str) -> Path:
        absolute = self._inside_root(path)
        if not absolute.is_file():
            raise SpecError(f"{field} file does not exist: {path}")
        return absolute

    def _require_directory(self, path: Path, *, field: str) -> Path:
        absolute = self._inside_root(path)
        if not absolute.is_dir():
            raise SpecError(f"{field} directory does not exist: {path}")
        return absolute


class LocalTargetResolver(LocalResourceLoader):
    """Resolve a repository chart directory or a named/explicit ``LocalStack``."""

    def resolve(self, target: str | Path) -> ResolvedLocalTarget:
        raw = str(target)
        if not raw or raw != raw.strip():
            raise SpecError("local target must be a non-empty chart path or stack name")
        candidate = Path(raw)
        explicit = candidate if candidate.is_absolute() else self.root / candidate
        if explicit.exists():
            return self._resolve_explicit(explicit)

        if candidate.is_absolute() or len(candidate.parts) != 1 or candidate.suffix:
            raise SpecError(f"local target path does not exist: {candidate}")
        try:
            name = _name(raw, field="LocalStack name")
        except ValueError as exc:
            raise SpecError(str(exc)) from exc
        stack_path = self.stacks_path / f"{name}.yaml"
        if not stack_path.is_file():
            raise SpecError(f"unknown LocalStack {name!r}: expected {stack_path}")
        resolved = self._resolve_stack(stack_path)
        if resolved.name != name:
            raise SpecError(
                f"{stack_path} metadata.name {resolved.name!r} does not match stack name {name!r}"
            )
        return resolved

    def _resolve_explicit(self, path: Path) -> ResolvedLocalTarget:
        absolute = self._inside_root(path)
        if absolute.is_dir():
            chart_yaml = absolute / "Chart.yaml"
            if not chart_yaml.is_file():
                raise SpecError(f"local target directory has no Chart.yaml: {path}")
            chart_document = load_yaml_file(chart_yaml)
            name = chart_document.get("name")
            if not isinstance(name, str):
                raise SpecError(f"{chart_yaml} must define a string name")
            try:
                _name(name, field="Chart.yaml name")
            except ValueError as exc:
                raise SpecError(f"invalid chart target {path}: {exc}") from exc
            return ResolvedChartTarget(name=name, path=absolute)
        if absolute.is_file():
            return self._resolve_stack(absolute)
        raise SpecError(f"local target is neither a chart directory nor LocalStack file: {path}")

    def _resolve_stack(self, path: Path) -> ResolvedStackTarget:
        if path.suffix not in {".yaml", ".yml"}:
            raise SpecError(f"LocalStack file must use .yaml or .yml: {path}")
        stack = self.load_stack(path)
        return ResolvedStackTarget(name=stack.metadata.name, path=path.resolve(), stack=stack)


def resolve_chart_target(
    root: Path,
    chart: str,
    *,
    charts_dir: Path = DEFAULT_CHARTS_DIR,
    local_config: Path = DEFAULT_LOCAL_CONFIG,
) -> ResolvedChartTarget:
    """Resolve a configured chart name or an explicit chart directory."""
    root = root.resolve()
    candidate = Path(chart)
    if len(candidate.parts) == 1 and not candidate.is_absolute():
        explicit = root / candidate
        if not explicit.exists():
            configured = root / charts_dir / candidate
            if configured.exists():
                candidate = configured
    resolved = LocalTargetResolver(root, local_config=local_config).resolve(candidate)
    if not isinstance(resolved, ResolvedChartTarget):
        raise SpecError(f"--chart must select a chart directory, not {resolved.kind}")
    return resolved


__all__ = [
    "DEFAULT_LOCAL_CLUSTER_FILE",
    "DEFAULT_LOCAL_CONFIG",
    "DEFAULT_STACKS_DIR",
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
    "LocalResourceLoader",
    "LocalStack",
    "LocalStackSpec",
    "LocalTargetResolver",
    "OciChartRelease",
    "ResolvedChartTarget",
    "ResolvedLocalTarget",
    "ResolvedStackTarget",
    "ResourceMetadata",
    "StackRelease",
    "WorkloadsReady",
    "load_local_cluster",
    "load_local_stack",
    "resolve_chart_target",
]
