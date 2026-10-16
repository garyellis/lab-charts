"""Turn authored bootstrap and stack releases into what Helm installs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from chart_manager.api.v1alpha1.releases import (
    LocalChartRelease,
    OciChartRelease,
    RepoChartRelease,
)
from chart_manager.shared.charts.install_plan import InstallPlanEntry
from chart_manager.shared.cluster.converge import Release


def release(entry: InstallPlanEntry, *, sets: Mapping[str, str]) -> Release:
    """The Helm release one install-plan entry installs, with runtime `--set` values."""
    return Release(
        name=entry.chart.name,
        chart=entry.chart.path,
        namespace=entry.namespace,
        values=entry.values,
        sets=sets,
        timeout=entry.spec.timeout,
    )


def helm_release(
    authored: LocalChartRelease | OciChartRelease | RepoChartRelease,
    root: Path,
    *,
    sets: Mapping[str, str] | None = None,
) -> tuple[Release, str]:
    """The Helm release a local, OCI or repository entry installs, and its row label."""
    base = Release(
        name=authored.name,
        chart="",
        namespace=authored.namespace,
        values=tuple(root / path for path in authored.values),
        sets=dict(sets or {}),
        timeout=authored.timeout,
    )
    if isinstance(authored, OciChartRelease):
        release = replace(base, chart=oci_chart_ref(authored), version=authored.version)
        return release, oci_identity(authored)
    if isinstance(authored, RepoChartRelease):
        release = replace(base, chart=authored.chart, version=authored.version, repo=authored.repo)
        return release, authored.version
    return replace(base, chart=(root / authored.chart).resolve()), "local"


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
    "oci_chart_ref",
    "oci_identity",
    "release",
]
