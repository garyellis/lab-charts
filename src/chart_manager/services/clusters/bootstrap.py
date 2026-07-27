"""Cluster bootstrap: install Cilium as the kind cluster's CNI.

The ephemeral test and persistent development cluster services use this same
path, so ``sandbox test`` and ``sandbox up`` cannot drift.

Cilium runs as the kind cluster CNI with full kube-proxy replacement, so it
must come up before anything else can become Ready. These bootstrap settings
live here -- not in ``chart-lifecycle.yaml`` -- because they are a property of
the kind environment, not of the Cilium chart's ``spec.clusterTest`` contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.progress import ProgressCallback, emit, info, step, warn

CILIUM_BOOTSTRAP_CHART = "cilium"
CILIUM_BOOTSTRAP_PROFILE = "minimal"
CILIUM_BOOTSTRAP_NAMESPACE = "kube-system"
CILIUM_BOOTSTRAP_TIMEOUT = "10m"
KIND_CONFIG_FILENAME = "kind-config.yaml"


def kind_config_path(root: Path) -> Path | None:
    """The repo-root kind-config.yaml, or None when it is absent.

    Single owner of the "use kind-config.yaml if the repo has one, else let
    kind use its own defaults" rule. Both `sandbox up`
    (DevelopmentClusterService) and `sandbox test`/`sandbox ensure`
    (EphemeralTestClusterService) create the same cluster,
    so they must agree on which config file it was created from.
    """
    config = root / KIND_CONFIG_FILENAME
    return config if config.exists() else None


def bootstrap(
    cluster_name: str,
    *,
    helm: Helm,
    kind: Kind,
    kubectl: Kubectl,
    cluster_tests: ClusterTestCatalog,
    progress: ProgressCallback | None = None,
    lint: bool = False,
) -> Literal["applied", "no-change"] | None:
    """Install / converge cilium as the kind cluster CNI.

    Returns:
      * The helm outcome ("applied" if a new release revision was produced,
        "no-change" if not).
      * `None` when the cilium chart is absent from the repository and
        bootstrap was skipped entirely.

    Converge semantics: this function always runs `helm upgrade --install`.
    If the release already exists with identical rendered manifests, helm
    no-ops it and we report "no-change" so callers can skip the rollout
    wait. The rollout wait still runs on "applied", matching the prior
    rollout-status gate on kube-system.
    """
    if CILIUM_BOOTSTRAP_CHART not in cluster_tests.repository.list_names():
        emit(progress, warn("cilium chart not found; skipping CNI bootstrap", label=None))
        return None
    chart = cluster_tests.get(CILIUM_BOOTSTRAP_CHART)

    api_ip = kind.control_plane_ip(cluster_name)
    values = cluster_tests.value_paths(chart, CILIUM_BOOTSTRAP_PROFILE)

    emit(
        progress,
        step(
            "Bootstrapping cilium CNI",
            f"(k8sServiceHost={api_ip}, namespace={CILIUM_BOOTSTRAP_NAMESPACE})",
        ),
    )
    # mtime-gated to skip the dep update when Chart.lock is fresh; same
    # cache as the lab install loop, so a single process never updates
    # the cilium chart's deps more than once.
    helm.dependency_update_if_stale(chart.path)
    if lint:
        helm.lint(chart.path, values)

    with _diagnostics_on_failure(kubectl, progress, CILIUM_BOOTSTRAP_NAMESPACE):
        result = helm.upgrade_install(
            CILIUM_BOOTSTRAP_CHART,
            chart.path,
            namespace=CILIUM_BOOTSTRAP_NAMESPACE,
            values=values,
            sets={
                "cilium.k8sServiceHost": api_ip,
                "cilium.k8sServicePort": "6443",
            },
            timeout=CILIUM_BOOTSTRAP_TIMEOUT,
            wait=False,
        )

    # Block until cilium-agent (daemonset) and coredns (deployment) are
    # rolled out -- coredns can only become Ready once cilium is wiring
    # pod networking, so this is also our "nodes are usable" gate. Skip
    # on no-change: nothing rolled, the wait would be a no-op anyway.
    if result.status == "applied":
        emit(progress, step("Waiting for kube-system workloads", "(cilium, coredns)"))
        kubectl.wait_workloads_ready(CILIUM_BOOTSTRAP_NAMESPACE, timeout=CILIUM_BOOTSTRAP_TIMEOUT)
    return result.status


@contextmanager
def _diagnostics_on_failure(
    kubectl: Kubectl, progress: ProgressCallback | None, namespace: str
) -> Iterator[None]:
    """Emit pod/event diagnostics on ExternalCommandError, then re-raise."""
    try:
        yield
    except ExternalCommandError:
        diagnostics = kubectl.diagnostics(namespace)
        if diagnostics.strip():
            emit(progress, info(diagnostics))
        raise
