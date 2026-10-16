"""Load the repository's `LocalCluster` and check every path it names exists inside the root.

Paths authored in these documents are repository-relative, so resolving one never reaches
outside the repository root.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

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
from chart_manager.plumbing.paths import inside_root, validate_hook_executable
from chart_manager.plumbing.yaml_files import load_yaml_file
from chart_manager.shared.charts.chart import load_chart
from chart_manager.shared.charts.lifecycle import (
    CapabilityStatus,
    chart_test_status,
    require_chart_test,
    require_chart_test_profile,
)
from chart_manager.shared.workspace import RepositoryWorkspace


def load_cluster(workspace: RepositoryWorkspace) -> LocalCluster:
    """Load the workspace's `LocalCluster` and check the files and charts it names."""
    root = workspace.root.resolve()
    cluster = load_resource(workspace.local_cluster_path, LocalCluster)
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


def load_resource[M: BaseModel](path: Path, model: type[M]) -> M:
    """Strictly load one `LocalCluster` or `LocalStack` document."""
    if not path.is_file():
        raise SpecError(f"local resource file does not exist: {path}")
    try:
        return model.model_validate(load_yaml_file(path))
    except (YamlError, ValueError) as exc:
        raise SpecError(f"invalid local resource {path}: {exc}") from exc


def validate_release(root: Path, release: BootstrapRelease | StackRelease) -> None:
    """Check the chart directory, its lifecycle and the values files a release names."""
    if isinstance(release, (LifecycleRelease, LocalChartRelease)):
        chart_dir = inside_root(root, release.chart)
        if not chart_dir.is_dir():
            raise SpecError(f"release.chart directory does not exist: {release.chart}")
        chart = load_chart(chart_dir)
        if isinstance(release, LocalChartRelease) and release.name != chart.name:
            raise SpecError(
                f"local release name {release.name!r} does not match "
                f"{chart_dir / 'Chart.yaml'} name {chart.name!r}"
            )
        if isinstance(release, LifecycleRelease):
            if chart_test_status(chart.lifecycle) is not CapabilityStatus.ENABLED:
                raise SpecError(
                    f"lifecycle release chart {release.chart} has no enabled chartTest"
                )
            chart_test = require_chart_test(chart.lifecycle, chart_name=chart.name)
            require_chart_test_profile(chart_test, release.profile)
    if isinstance(release, (LocalChartRelease, OciChartRelease, RepoChartRelease)):
        for path in release.values:
            require_file(root, path, field="release.values[]")


def require_file(root: Path, path: Path, *, field: str) -> Path:
    """`path` inside `root`, which must be an existing file."""
    absolute = inside_root(root, path)
    if not absolute.is_file():
        raise SpecError(f"{field} file does not exist: {path}")
    return absolute
