"""Load repository-local authored resources and resolve them against the tree.

The accepted shape of ``LocalCluster`` and ``LocalStack`` is owned by
``chart_manager.api.local.v1alpha1``.  What is left here is everything that
needs more than a single document: reading the YAML and translating decode
failures into ``SpecError``, keeping every referenced path inside the
repository root, checking that charts, ``Chart.yaml`` files and values files
actually exist, agreeing a release name with the chart it names, and resolving
a command-line target to either a chart directory or a loaded stack.

Paths authored in those documents are repository-relative by construction, so
resolving one never grants access outside the repository root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from chart_manager.api.local.v1alpha1 import (
    BootstrapRelease,
    LifecycleRelease,
    LocalChartRelease,
    LocalCluster,
    LocalStack,
    OciChartRelease,
    RepoChartRelease,
    StackRelease,
)
from chart_manager.domain.lifecycle_policy import (
    LIFECYCLE_FILENAME,
    load_chart_lifecycle,
    require_cluster_test_profile,
)
from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.names import dns_label
from chart_manager.plumbing.paths import relative_path
from chart_manager.plumbing.yaml_files import load_yaml_file
from chart_manager.settings import DEFAULT_CHARTS_DIR, DEFAULT_LOCAL_CONFIG

DEFAULT_STACKS_DIR = Path("stacks")


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


class ResolvedChartTarget(BaseModel):
    """An explicit chart directory selected as a local target."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["chart"] = "chart"
    name: str
    path: Path


class ResolvedStackTarget(BaseModel):
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
        self.local_config = relative_path(local_config, field="local_config")
        self.stacks_dir = relative_path(stacks_dir, field="stacks_dir")

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
                lifecycle_path = chart / LIFECYCLE_FILENAME
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
                require_cluster_test_profile(cluster_test, release.profile)
        if isinstance(release, (LocalChartRelease, OciChartRelease, RepoChartRelease)):
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
            name = dns_label(raw, field="LocalStack name")
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
                dns_label(name, field="Chart.yaml name")
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
    "DEFAULT_STACKS_DIR",
    "LocalResourceLoader",
    "LocalTargetResolver",
    "ResolvedChartTarget",
    "ResolvedLocalTarget",
    "ResolvedStackTarget",
    "load_local_cluster",
    "load_local_stack",
    "resolve_chart_target",
]
