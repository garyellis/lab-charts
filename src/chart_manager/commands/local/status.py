"""Read-only snapshot of the development cluster: what exists, and where.

Every lookup here already existed inside the converge path -- `helm list -A`
is the install-skip snapshot, the URL list
is `access.virtualservice_urls`, the port diff is `drift.port_mapping_drift`.
`status` asks the same questions and keeps the answers instead of consuming
them, which is why this module composes those helpers rather than reaching
for the adapters a second time.

Best-effort in the same sense as `access.py`: a stopped cluster or an
unreachable apiserver is *the answer*, so a failed lookup is captured as an
error string on the result rather than raised. The one thing that would make
the report meaningless -- not knowing whether the cluster exists -- is
established first, and everything cluster-facing is skipped when it does not.
"""

from __future__ import annotations

from pathlib import Path

from chart_manager.commands.local.access import virtualservice_urls
from chart_manager.commands.local.drift import port_mapping_drift
from chart_manager.commands.local.models import (
    DevelopmentClusterRelease,
    DevelopmentClusterStatus,
)
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.shared.cluster.session import Session


def cluster_status(
    lab: Session | None,
    *,
    name: str,
    kind: Kind,
    root: Path,
    config: Path | None = None,
) -> DevelopmentClusterStatus:
    """Collect the current state of the cluster, or report that it does not exist.

    Existence gates the rest: asking Helm about an absent cluster only produces a
    kubeconfig error that says nothing `exists: false` did not.
    """
    if lab is None:
        return DevelopmentClusterStatus(cluster_name=name, exists=False)
    releases, releases_error = _releases(lab.helm)
    urls, urls_error = _urls(lab.kubectl)
    return DevelopmentClusterStatus(
        cluster_name=name,
        exists=True,
        context=lab.context,
        provider="kind",
        releases=releases,
        releases_error=releases_error,
        urls=urls,
        urls_error=urls_error,
        drift=port_mapping_drift(name, kind=kind, root=root, config=config),
    )


def _releases(helm: Helm) -> tuple[tuple[DevelopmentClusterRelease, ...], str | None]:
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
            DevelopmentClusterRelease(
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
