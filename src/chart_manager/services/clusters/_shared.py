"""Authored-configuration resolution shared by the three converge paths.

`bootstrap.py`, `development/service.py` and `ephemeral.py` each turn the same
authored documents into the same four answers: the name a chart directory
declares, the install plan behind a lifecycle release, the Helm reference
behind a pinned OCI release, and the resolved Kind config path. Each carried
its own copy, and the copies had already drifted -- two of them raised
differently worded errors for the identical unresolvable state, and the
default namespace forked outright (see `DEFAULT_NAMESPACE` below).

Deliberately named for what it is rather than for a concept it does not have:
this module holds exactly the duplication removed from those three modules,
not a home for new cluster framework. Nothing here may import them back.
"""

from __future__ import annotations

from pathlib import Path

from chart_manager.api.local.v1alpha1 import (
    LifecycleRelease,
    LocalCluster,
    OciChartRelease,
)
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.yaml_files import load_yaml_file
from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.domain.install_plan import DependencyResolver, InstallPlanEntry

#: Namespace for a cluster-test profile that declares no `namespace:`, on the
#: bootstrap and development converge paths (`local up`, `local plan`,
#: `local reset`, and every LocalCluster bootstrap release in *both*
#: services).
#:
#: This value is load-bearing beyond being a default: bootstrap publishes its
#: ownership as `ExternallySatisfiedLifecycle` identities that include the
#: namespace, and `_preflight_target` excludes workload entries by exact
#: identity. Resolve the two sides against different defaults and a
#: bootstrap-owned chart gets converged a second time.
#:
#: Deliberately *not* the same value as `ephemeral.DEFAULT_NAMESPACE`
#: ("observability"); the fork is real and documented at that constant.
DEFAULT_NAMESPACE = "default"


def chart_name(root: Path, chart_relative: Path) -> str:
    """The `name:` a repository-relative chart directory declares."""
    document = load_yaml_file(root / chart_relative / "Chart.yaml")
    name = document.get("name")
    if not isinstance(name, str):
        raise ChartManagerError(f"{chart_relative}/Chart.yaml must define a string name")
    return name


def lifecycle_install_plan(
    root: Path,
    release: LifecycleRelease,
    *,
    source: str,
) -> tuple[ClusterTestCatalog, list[InstallPlanEntry]]:
    """Resolve one lifecycle release to its catalog and ordered install plan.

    The catalog is anchored at the release's own parent directory rather than
    the repository-wide charts dir, and the path-identity check is what makes
    that safe: a chart directory whose Chart.yaml declares a name owned by
    some other tree would otherwise install that other tree under this
    release's identity.

    `source` names the caller in the mismatch error -- the wording of that one
    message is the only thing the two copies of this function differed by.
    """
    name = chart_name(root, release.chart)
    catalog = ClusterTestCatalog(root, charts_dir=release.chart.parent)
    chart = catalog.get(name)
    if chart.path.resolve() != (root / release.chart).resolve():
        raise ChartManagerError(
            f"{source} {name!r} does not match its chart-lifecycle chart: "
            f"{release.chart} resolved to {chart.path}"
        )
    return catalog, DependencyResolver(catalog.get).install_plan(name, release.profile)


def oci_identity(release: OciChartRelease) -> str:
    """How a pinned OCI release is identified in progress and report rows.

    The API model lets both pins be absent (the reference carries its own
    tag), and every consumer here fills a column that has to say something,
    so the bare word is the last resort rather than an empty cell.
    """
    return release.version or release.digest or "pinned"


def oci_chart_ref(release: OciChartRelease) -> str:
    """The Helm reference for a pinned OCI release.

    Only the digest goes in the reference: `helm` takes a version as the
    `--version` flag, which the callers pass separately.
    """
    if release.digest is None:
        return release.chart
    return f"{release.chart}@{release.digest}"


def kind_config_path(root: Path, local_cluster: LocalCluster) -> Path:
    """The LocalCluster's authored Kind config, resolved against the root."""
    return (root / local_cluster.spec.cluster.config).resolve()


__all__ = [
    "DEFAULT_NAMESPACE",
    "chart_name",
    "kind_config_path",
    "lifecycle_install_plan",
    "oci_chart_ref",
    "oci_identity",
]
