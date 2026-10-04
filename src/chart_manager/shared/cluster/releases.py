"""Turn authored bootstrap and stack releases into what Helm installs."""

from __future__ import annotations

from pathlib import Path

from chart_manager.api.v1alpha1.releases import LifecycleRelease, OciChartRelease
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.shared.charts.chart import load_chart_name
from chart_manager.shared.charts.chart_tests import ChartTestCatalog
from chart_manager.shared.charts.install_plan import DependencyResolver, InstallPlanEntry


def chart_name(root: Path, chart_relative: Path) -> str:
    """The `name:` a repository-relative chart directory declares."""
    return load_chart_name(root / chart_relative / "Chart.yaml")


def lifecycle_install_plan(
    root: Path,
    release: LifecycleRelease,
    *,
    source: str,
) -> tuple[ChartTestCatalog, list[InstallPlanEntry]]:
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
    catalog = ChartTestCatalog(root, charts_dir=release.chart.parent)
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


__all__ = [
    "chart_name",
    "lifecycle_install_plan",
    "oci_chart_ref",
    "oci_identity",
]
