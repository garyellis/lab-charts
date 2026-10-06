"""Read-only snapshot of the dev cluster: whether it exists, its releases, URLs and drift.

A failed lookup is recorded on the result, never raised; nothing is asked of a cluster
that does not exist.
"""

from __future__ import annotations

from pathlib import Path

from chart_manager.commands.local.access import virtualservice_urls
from chart_manager.commands.local.drift import port_mapping_drift
from chart_manager.commands.local.models import (
    DevClusterRelease,
    DevClusterStatus,
)
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.shared.cluster.session import Session


def cluster_status(
    session: Session | None,
    *,
    name: str,
    kind: Kind,
    root: Path,
    config: Path | None = None,
) -> DevClusterStatus:
    """Collect the current state of the cluster, or report that it does not exist.

    Existence gates the rest: asking Helm about an absent cluster only produces a
    kubeconfig error that says nothing `exists: false` did not.
    """
    if session is None:
        return DevClusterStatus(cluster_name=name, exists=False)
    releases, releases_error = _releases(session.helm)
    urls, urls_error = _urls(session.kubectl)
    return DevClusterStatus(
        cluster_name=name,
        exists=True,
        context=session.context,
        provider="kind",
        releases=releases,
        releases_error=releases_error,
        urls=urls,
        urls_error=urls_error,
        drift=port_mapping_drift(name, kind=kind, root=root, config=config),
    )


def _releases(helm: Helm) -> tuple[tuple[DevClusterRelease, ...], str | None]:
    """Every Helm release on the cluster, ordered for a stable report.

    Sorted by (namespace, name) rather than left in Helm's order: this is a
    document a caller diffs between runs, and `helm list -A` orders by
    whatever the storage driver hands back.
    """
    try:
        found = helm.list_releases(all_namespaces=True)
    except ChartManagerError as exc:
        return (), f"could not list helm releases ({exc})"
    return (
        tuple(
            DevClusterRelease(
                name=release.name,
                namespace=release.namespace,
                revision=release.revision,
                status=release.status,
            )
            for release in sorted(found, key=lambda r: (r.namespace, r.name))
        ),
        None,
    )


def _urls(kubectl: Kubectl) -> tuple[tuple[str, ...], str | None]:
    """The reachable URLs, through the same projection `local up` prints."""
    try:
        virtualservices = kubectl.list_virtualservices()
    except ChartManagerError as exc:
        return (), f"could not list VirtualServices ({exc}); skipping URL hints"
    return virtualservice_urls(virtualservices), None


__all__ = ["cluster_status"]
