"""Pure lifecycle-plan projections owned by the execution environment."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from chart_manager.services.lifecycle.models import (
    ActionKind,
    LifecycleAction,
    LifecyclePlan,
)

EXTERNAL_BOOTSTRAP_WARNING_PREFIX = "environment bootstrap externally satisfies chart(s): "


class PlanProjectionError(ValueError):
    """The requested environment projection is invalid for the supplied plan."""


@dataclass(frozen=True)
class ExternallySatisfiedLifecycle:
    """Exact managed lifecycle identity already converged by an environment."""

    chart_path: Path
    chart: str
    profile: str
    namespace: str


def exclude_bootstrap_owned_charts(
    plan: LifecyclePlan,
    bootstrap_lifecycles: Iterable[ExternallySatisfiedLifecycle],
) -> LifecyclePlan:
    """Project environment-bootstrap ownership onto a cluster plan.

    Transitive bootstrap charts are removed completely. If bootstrap owns the
    requested target, readiness and Helm tests remain so the requested profile
    is still verified without fabricating install work or evidence. Remaining
    action order is unchanged, so compiler ordering remains authoritative.
    """

    externally_satisfied = frozenset(bootstrap_lifecycles)
    if any(
        not identity.chart.strip()
        or not identity.profile.strip()
        or not identity.namespace.strip()
        for identity in externally_satisfied
    ):
        raise PlanProjectionError("bootstrap lifecycle identity fields must not be empty")

    def is_satisfied(action: LifecycleAction) -> bool:
        target = action.target
        if target.profile is None or target.namespace is None:
            return False
        identity = ExternallySatisfiedLifecycle(
            chart_path=action.chart_path.resolve(),
            chart=target.chart,
            profile=target.profile,
            namespace=target.namespace,
        )
        return identity in externally_satisfied

    removed_ids = frozenset(
        action.action_id
        for action in plan.actions
        if is_satisfied(action)
        and (
            action.target.chart != plan.chart
            or action.target.profile != plan.profile
            or action.kind
            not in (ActionKind.WORKLOAD_READY, ActionKind.HELM_TEST)
        )
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
    warning = (
        EXTERNAL_BOOTSTRAP_WARNING_PREFIX
        + ", ".join(removed_charts)
        + "; environment-owned preparation/install actions were excluded "
        "from this executable plan"
    )
    return replace(
        plan,
        actions=actions,
        warnings=(*plan.warnings, warning),
    )
