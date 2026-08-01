"""Read-only snapshot of the development cluster: what exists, and where.

Every lookup here already existed inside the converge path -- `helm list -A`
is the install-skip snapshot (`service._existing_release_keys`), the URL list
is `access.urls_and_grafana_host`, the port diff is `drift.port_mapping_drift`.
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

from collections.abc import Callable
from pathlib import Path

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.clusters.development.access import urls_and_grafana_host
from chart_manager.services.clusters.development.drift import port_mapping_drift
from chart_manager.services.clusters.development.models import (
    DevelopmentClusterRelease,
    DevelopmentClusterStatus,
)
from chart_manager.services.clusters.environment import (
    EnvironmentHandle,
    EnvironmentSpec,
    KubernetesEnvironmentProvider,
)
from chart_manager.services.expose import ExposeService

#: Bind the cluster-facing clients to a resolved environment. The same shape
#: `DevelopmentClusterService` hands `_ensure_environment`.
ClientFactory = Callable[[EnvironmentHandle], tuple[Helm, Kubectl, ExposeService]]


def cluster_status(
    cluster_name: str,
    *,
    clients: ClientFactory,
    kind: Kind,
    environment_provider: KubernetesEnvironmentProvider,
    root: Path,
    config: Path | None = None,
) -> DevelopmentClusterStatus:
    """Collect the current state of the named development cluster.

    `provider.inspect` is the existence question -- it is the same call
    `up` would make to decide whether to create -- and it gates the rest:
    querying Helm against an absent cluster produces a kubeconfig error that
    says nothing the `exists: false` did not already say, only louder.

    Clients are built *from the resolved handle* rather than taken as
    arguments, for the same reason `up` rebinds them after ensuring the
    environment: an unbound Helm answers about whatever kubecontext the
    workstation happens to be pointing at, so `local status` would
    confidently report a production cluster's releases as the local lab's.

    The port-forward is read from the state file rather than probed, so this
    stays free of the "is it really alive" cost `ExposeService.status`
    already pays and no more.
    """
    handle = environment_provider.inspect(
        EnvironmentSpec(name=cluster_name, cluster_name=cluster_name)
    )
    if handle is None:
        return DevelopmentClusterStatus(cluster_name=cluster_name, exists=False)

    helm, kubectl, expose = clients(handle)
    releases, releases_error = _releases(helm)
    urls, urls_error = _urls(kubectl)
    forward = expose.status(cluster_name)
    return DevelopmentClusterStatus(
        cluster_name=cluster_name,
        exists=True,
        context=handle.context,
        provider=handle.provider_type,
        releases=releases,
        releases_error=releases_error,
        urls=urls,
        urls_error=urls_error,
        port_forward_pid=None if forward is None else forward.pid,
        drift=port_mapping_drift(cluster_name, kind=kind, root=root, config=config),
    )


def _releases(helm: Helm) -> tuple[tuple[DevelopmentClusterRelease, ...], str | None]:
    """Every Helm release on the cluster, ordered for a stable report.

    Sorted by (namespace, name) rather than left in Helm's order: this is a
    document a caller diffs between runs, and `helm list -A` orders by
    whatever the storage driver hands back.
    """
    try:
        found = helm.list_releases(all_namespaces=True)
    except (ExternalCommandError, ChartManagerError) as exc:
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
        hosts = kubectl.list_virtualservice_hosts()
    except (ExternalCommandError, ChartManagerError) as exc:
        return (), f"could not list VirtualServices ({exc}); skipping URL hints"
    urls, _grafana_host = urls_and_grafana_host(hosts)
    return urls, None


__all__ = ["cluster_status"]
