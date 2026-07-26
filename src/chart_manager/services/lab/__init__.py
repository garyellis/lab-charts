"""LabService -- long-lived dev cluster lifecycle for the full stack.

Contrast with `SandboxService`:
  - SandboxService = ephemeral, one chart's smoke test, fail-fast, runs
    `helm test` per chart. CI-shaped.
  - LabService     = persistent, the whole observability stack from the
    grafana-dashboards `prototyping` profile, continue-on-error so a single
    flaky chart doesn't block iteration on the rest, NO `helm test` calls.
    Developer-shaped.

Lifecycle verbs (surfaced as `chart-manager sandbox up|down|delete`):

  - up     : create or start the cluster, then install the stack.
             `Kind.ensure_cluster` handles the absent/stopped/running cases,
             so `up` works whether the cluster has never been created, was
             stopped via `sandbox down`, or is already running.
  - down   : `docker stop` the cluster's node containers. Preserves etcd,
             installed Helm releases, PVCs, and the containerd image cache.
             Fast restart via `up`. No image re-pull.
  - delete : `kind delete cluster` -- full teardown, image cache and
             release state are gone, next `up` re-pulls everything.

The persistent cluster is intentionally named `chart-manager` (the same name
SandboxService uses): one human developer is not running both at once, and
sharing the name lets `sandbox test` and `sandbox up/down/delete` cooperate
on the same kind cluster.

Readiness contract is rollout-status only (Kubectl.wait_workloads_ready),
the same gate SandboxService uses between install and `helm test`.

Package layout -- three collaborator sets, three modules:

  - `models.py`  : the frozen request/result vocabulary + the one mutable
                   accumulator. No collaborators at all.
  - `access.py`  : "what can I reach, and is the CA trusted?" -- kubectl +
                   progress only.
  - `drift.py`   : declared-vs-live cluster shape -- kind + helm + progress.
  - `service.py` : the converge engine, which needs all of the above plus
                   the chart repository and the dependency resolver.

This module is the package's public face. It re-exports exactly the
module-level surface the single-file `services/lab.py` had, no more: the
models, the service, the constants, and `cluster_bootstrap` (which the lab
tests monkeypatch through this module). The import path
(`chart_manager.services.lab`) is therefore unchanged -- the split is
internal, and `composition.py` did not have to move.

The functions in `access.py` / `drift.py` are deliberately NOT re-exported
here. They were methods before, so nothing imports them from the package
root; importing them from `chart_manager.services.lab.access` names where
they actually live.
"""
from __future__ import annotations

from chart_manager.services import cluster_bootstrap
from chart_manager.services.cluster_bootstrap import (
    CILIUM_BOOTSTRAP_CHART,
    CILIUM_BOOTSTRAP_NAMESPACE,
)
from chart_manager.services.lab.access import (
    APPS_WILDCARD_CERT_NAME,
    APPS_WILDCARD_CERT_NAMESPACE,
    APPS_WILDCARD_CERT_TIMEOUT,
    GRAFANA_ADMIN_SECRET_KEY,
    GRAFANA_ADMIN_USER,
    GRAFANA_RELEASE,
    LAB_CA_OWNER_CHART,
    LAB_CA_SECRET_NAME,
    LAB_CA_SECRET_NAMESPACE,
)
from chart_manager.services.lab.drift import CILIUM_SERVICE_HOST_PATH
from chart_manager.services.lab.models import (
    DEFAULT_CHART,
    DEFAULT_CLUSTER_NAME,
    DEFAULT_NAMESPACE,
    DEFAULT_PROFILE,
    AccessHints,
    ClusterActionResult,
    EntryFailure,
    EntryOutcome,
    LabResult,
    LabSyncOptions,
    LabUpOptions,
    _RunSummary,
)
from chart_manager.services.lab.service import (
    CERT_MANAGER_CHART,
    CERT_MANAGER_WEBHOOK_DEPLOYMENT,
    CERT_MANAGER_WEBHOOK_NAMESPACE,
    CERT_MANAGER_WEBHOOK_TIMEOUT,
    LabService,
)

__all__ = [
    "APPS_WILDCARD_CERT_NAME",
    "APPS_WILDCARD_CERT_NAMESPACE",
    "APPS_WILDCARD_CERT_TIMEOUT",
    "CERT_MANAGER_CHART",
    "CERT_MANAGER_WEBHOOK_DEPLOYMENT",
    "CERT_MANAGER_WEBHOOK_NAMESPACE",
    "CERT_MANAGER_WEBHOOK_TIMEOUT",
    "CILIUM_BOOTSTRAP_CHART",
    "CILIUM_BOOTSTRAP_NAMESPACE",
    "CILIUM_SERVICE_HOST_PATH",
    "DEFAULT_CHART",
    "DEFAULT_CLUSTER_NAME",
    "DEFAULT_NAMESPACE",
    "DEFAULT_PROFILE",
    "GRAFANA_ADMIN_SECRET_KEY",
    "GRAFANA_ADMIN_USER",
    "GRAFANA_RELEASE",
    "LAB_CA_OWNER_CHART",
    "LAB_CA_SECRET_NAME",
    "LAB_CA_SECRET_NAMESPACE",
    "AccessHints",
    "ClusterActionResult",
    "EntryFailure",
    "EntryOutcome",
    "LabResult",
    "LabService",
    "LabSyncOptions",
    "LabUpOptions",
    # Private, but `tests/test_lab_access_hints.py` builds one directly to
    # drive the access-hint resolution without a full converge.
    "_RunSummary",
    "cluster_bootstrap",
]
