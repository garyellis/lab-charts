"""Persistent development-cluster convergence and lifecycle.

This service remains behaviorally distinct from ephemeral cluster testing:
it converges a composed target, continues after per-release failures, and
does not run Helm tests.
"""

from chart_manager.services.clusters.development.access import (
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
from chart_manager.services.clusters.development.models import (
    DevelopmentClusterAccessHints,
    DevelopmentClusterActionResult,
    DevelopmentClusterEntryFailure,
    DevelopmentClusterEntryOutcome,
    DevelopmentClusterPlan,
    DevelopmentClusterPlanEntry,
    DevelopmentClusterRelease,
    DevelopmentClusterResult,
    DevelopmentClusterStatus,
    PortMappingDrift,
    RunSummary,
)
from chart_manager.services.clusters.development.service import (
    CERT_MANAGER_CHART,
    CERT_MANAGER_WEBHOOK_DEPLOYMENT,
    CERT_MANAGER_WEBHOOK_NAMESPACE,
    CERT_MANAGER_WEBHOOK_TIMEOUT,
    DevelopmentClusterService,
)
from chart_manager.services.clusters.development.wire import (
    action_to_dict,
    converge_to_dict,
    plan_to_dict,
    status_to_dict,
)

__all__ = [
    "APPS_WILDCARD_CERT_NAME",
    "APPS_WILDCARD_CERT_NAMESPACE",
    "APPS_WILDCARD_CERT_TIMEOUT",
    "CERT_MANAGER_CHART",
    "CERT_MANAGER_WEBHOOK_DEPLOYMENT",
    "CERT_MANAGER_WEBHOOK_NAMESPACE",
    "CERT_MANAGER_WEBHOOK_TIMEOUT",
    "GRAFANA_ADMIN_SECRET_KEY",
    "GRAFANA_ADMIN_USER",
    "GRAFANA_RELEASE",
    "LAB_CA_OWNER_CHART",
    "LAB_CA_SECRET_NAME",
    "LAB_CA_SECRET_NAMESPACE",
    "DevelopmentClusterAccessHints",
    "DevelopmentClusterActionResult",
    "DevelopmentClusterEntryFailure",
    "DevelopmentClusterEntryOutcome",
    "DevelopmentClusterPlan",
    "DevelopmentClusterPlanEntry",
    "DevelopmentClusterRelease",
    "DevelopmentClusterResult",
    "DevelopmentClusterService",
    "DevelopmentClusterStatus",
    "PortMappingDrift",
    "RunSummary",
    "action_to_dict",
    "converge_to_dict",
    "plan_to_dict",
    "status_to_dict",
]
