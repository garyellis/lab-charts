"""Pure lifecycle-plan projections owned by the execution environment."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from chart_manager.services.lifecycle.models import LifecyclePlan, Workflow

EXTERNAL_BOOTSTRAP_WARNING_PREFIX = "environment bootstrap externally satisfies chart(s): "


class PlanProjectionError(ValueError):
    """The requested environment projection is invalid for the supplied plan."""


def exclude_bootstrap_owned_charts(
    plan: LifecyclePlan,
    bootstrap_charts: Iterable[str],
) -> LifecyclePlan:
    """Remove environment-bootstrap-owned chart actions from a cluster plan.

    Every edge incident to a removed action is removed as well.  The remaining
    action and edge tuple order is unchanged, so the compiler's deterministic
    ordering remains authoritative.  A warning on the returned plan makes the
    external satisfaction contract visible to ``plan``/``explain`` surfaces.
    """

    if plan.workflow is not Workflow.CLUSTER_TEST:
        raise PlanProjectionError(
            "bootstrap-owned chart projection requires a cluster-test plan"
        )

    requested = frozenset(bootstrap_charts)
    if any(not chart.strip() for chart in requested):
        raise PlanProjectionError("bootstrap-owned chart names must not be empty")
    if plan.chart in requested:
        raise PlanProjectionError(
            f"cannot exclude requested target chart {plan.chart!r} as bootstrap-owned"
        )

    removed_ids = frozenset(
        action.action_id for action in plan.actions if action.target.chart in requested
    )
    if not removed_ids:
        return plan

    removed_charts = tuple(
        sorted(
            {
                action.target.chart
                for action in plan.actions
                if action.action_id in removed_ids
            }
        )
    )
    actions = tuple(action for action in plan.actions if action.action_id not in removed_ids)
    edges = tuple(
        edge
        for edge in plan.edges
        if edge.source not in removed_ids and edge.target not in removed_ids
    )
    warning = (
        EXTERNAL_BOOTSTRAP_WARNING_PREFIX
        + ", ".join(removed_charts)
        + "; actions and incident edges were excluded from this executable plan"
    )
    return replace(
        plan,
        actions=actions,
        edges=edges,
        warnings=(*plan.warnings, warning),
    )
