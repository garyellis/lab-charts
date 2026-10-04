"""Load the repository's `LocalCluster` and check every path it names exists inside the root.

Paths authored in these documents are repository-relative, so resolving one never reaches
outside the repository root.
"""

from __future__ import annotations

from pathlib import Path

from chart_manager.api.v1alpha1.local_cluster import LocalCluster
from chart_manager.api.v1alpha1.releases import (
    BootstrapRelease,
    LifecycleRelease,
    LocalChartRelease,
    OciChartRelease,
    RepoChartRelease,
    StackRelease,
)
from chart_manager.plumbing.errors import SpecError, YamlError
from chart_manager.plumbing.paths import validate_hook_executable
from chart_manager.plumbing.yaml_files import load_yaml_file
from chart_manager.shared.charts.chart import load_chart_metadata
from chart_manager.shared.charts.lifecycle import (
    LIFECYCLE_FILENAME,
    load_chart_lifecycle,
    require_cluster_test_profile,
)
from chart_manager.shared.workspace import RepositoryWorkspace


def load_cluster(workspace: RepositoryWorkspace) -> LocalCluster:
    """Load the workspace's `LocalCluster` and check the files and charts it names."""
    root = workspace.root.resolve()
    cluster = load_local_cluster(workspace.local_cluster_path)
    require_file(root, cluster.spec.cluster.config, field="spec.cluster.config")
    hooks = cluster.spec.cluster.hooks
    if hooks is not None:
        for phase, command in (
            ("preProvision", hooks.pre_provision),
            ("postProvision", hooks.post_provision),
        ):
            if command is not None:
                validate_hook_executable(root, command[0], field=f"spec.cluster.hooks.{phase}[0]")
    for release in cluster.spec.bootstrap.releases:
        validate_release(root, release)
    return cluster


def load_local_cluster(path: Path) -> LocalCluster:
    """Strictly load one `LocalCluster` document."""
    if not path.is_file():
        raise SpecError(f"local resource file does not exist: {path}")
    try:
        return LocalCluster.model_validate(load_yaml_file(path))
    except (YamlError, ValueError) as exc:
        raise SpecError(f"invalid local resource {path}: {exc}") from exc


def validate_release(root: Path, release: BootstrapRelease | StackRelease) -> None:
    """Check the chart directory, its lifecycle and the values files a release names."""
    if isinstance(release, (LifecycleRelease, LocalChartRelease)):
        chart = inside_root(root, release.chart)
        if not chart.is_dir():
            raise SpecError(f"release.chart directory does not exist: {release.chart}")
        chart_yaml = chart / "Chart.yaml"
        if not chart_yaml.is_file():
            raise SpecError(f"release.chart has no Chart.yaml: {release.chart}")
        chart_name = load_chart_metadata(chart_yaml).name
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
            require_file(root, path, field="release.values[]")


def inside_root(root: Path, path: Path) -> Path:
    """`path` resolved against `root`; raise if it escapes the root."""
    resolved = (path if path.is_absolute() else root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise SpecError(f"path escapes repository root {root}: {path}")
    return resolved


def require_file(root: Path, path: Path, *, field: str) -> Path:
    """`path` inside `root`, which must be an existing file."""
    absolute = inside_root(root, path)
    if not absolute.is_file():
        raise SpecError(f"{field} file does not exist: {path}")
    return absolute
