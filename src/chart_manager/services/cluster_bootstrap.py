"""Cluster bootstrap: install Cilium as the kind cluster's CNI.

Extracted from SandboxService so that both `sandbox test` and `sandbox up`
share the exact same CNI bootstrap path. Behavior must be identical to the
inline implementation that previously lived in `services/sandbox.py`.

Cilium runs as the kind cluster CNI with full kube-proxy replacement, so it
must come up before anything else can become Ready. These bootstrap settings
live here -- not in test-spec.yaml -- because they are a property of the kind
environment, not of the cilium chart's test contract.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from rich.console import Console

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.charts import ChartRepository
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError

CILIUM_BOOTSTRAP_CHART = "cilium"
CILIUM_BOOTSTRAP_PROFILE = "minimal"
CILIUM_BOOTSTRAP_NAMESPACE = "kube-system"
CILIUM_BOOTSTRAP_TIMEOUT = "10m"
KIND_CONFIG_FILENAME = "kind-config.yaml"


def bootstrap(
    cluster_name: str,
    *,
    helm: Helm,
    kind: Kind,
    kubectl: Kubectl,
    repository: ChartRepository,
    console: Console,
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
    try:
        chart = repository.get(CILIUM_BOOTSTRAP_CHART)
    except ChartManagerError:
        console.print("[yellow]cilium chart not found; skipping CNI bootstrap[/yellow]")
        return None

    api_ip = kind.control_plane_ip(cluster_name)
    values = repository.value_paths(chart, CILIUM_BOOTSTRAP_PROFILE)

    console.print(
        f"[bold]Bootstrapping cilium CNI[/bold] "
        f"(k8sServiceHost={api_ip}, namespace={CILIUM_BOOTSTRAP_NAMESPACE})"
    )
    # mtime-gated to skip the dep update when Chart.lock is fresh; same
    # cache as the lab install loop, so a single process never updates
    # the cilium chart's deps more than once.
    helm.dependency_update_if_stale(chart.path)
    if lint:
        helm.lint(chart.path, values)

    with _diagnostics_on_failure(kubectl, console, CILIUM_BOOTSTRAP_NAMESPACE):
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
        console.print("[bold]Waiting for kube-system workloads[/bold] (cilium, coredns)")
        kubectl.wait_workloads_ready(
            CILIUM_BOOTSTRAP_NAMESPACE, timeout=CILIUM_BOOTSTRAP_TIMEOUT
        )
    return result.status


@contextmanager
def _diagnostics_on_failure(
    kubectl: Kubectl, console: Console, namespace: str
) -> Iterator[None]:
    try:
        yield
    except ExternalCommandError:
        diagnostics = kubectl.diagnostics(namespace)
        if diagnostics.strip():
            console.print(diagnostics)
        raise
