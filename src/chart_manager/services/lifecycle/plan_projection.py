"""Pure lifecycle-plan projections owned by the execution environment."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from chart_manager.services.lifecycle.models import (
    ActionKind,
    LifecycleAction,
    LifecyclePlan,
)
from chart_manager.shared.cluster.bootstrap import ExternallySatisfiedLifecycle

EXTERNAL_BOOTSTRAP_WARNING_PREFIX = "environment bootstrap externally satisfies chart(s): "
SKIPPED_REQUIRES_WARNING_PREFIX = "requires assumed installed (--skip-requires): "


class PlanProjectionError(ValueError):
    """The requested environment projection is invalid for the supplied plan."""


@dataclass(frozen=True)
class SkippedRequiredLifecycle:
    """One required release omitted from an executable cluster-test plan."""

    chart: str
    profile: str
    release: str
    namespace: str


@dataclass(frozen=True)
class RequiredLifecycleProjection:
    """A projected plan plus the releases a real run must preflight."""

    plan: LifecyclePlan
    skipped: tuple[SkippedRequiredLifecycle, ...]


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


def exclude_required_lifecycles(
    plan: LifecyclePlan,
    requested: Iterable[tuple[str, str]],
) -> RequiredLifecycleProjection:
    """Remove every action that belongs only to a ``requires`` entry.

    The compiler remains authoritative and produces the complete dependency
    graph. This environment projection keeps only explicitly selected test
    targets, so a target selected by ``--dependent-tests`` still runs in full
    even when another selected target also names it as a requirement.
    """

    selected = frozenset(requested)
    if not selected or any(
        not chart.strip() or not profile.strip() for chart, profile in selected
    ):
        raise PlanProjectionError("requested lifecycle identity fields must not be empty")

    skipped_by_identity: dict[tuple[str, str], SkippedRequiredLifecycle] = {}
    for action in plan.actions:
        profile = action.target.profile
        if profile is None or (action.target.chart, profile) in selected:
            continue
        release = action.target.release
        namespace = action.target.namespace
        if not release or not namespace:
            raise PlanProjectionError(
                f"required lifecycle {action.target.chart}:{profile} "
                "must identify a release and namespace"
            )
        identity = (action.target.chart, profile)
        candidate = SkippedRequiredLifecycle(
            chart=action.target.chart,
            profile=profile,
            release=release,
            namespace=namespace,
        )
        previous = skipped_by_identity.setdefault(identity, candidate)
        if previous != candidate:
            raise PlanProjectionError(
                f"required lifecycle {action.target.chart}:{profile} has conflicting coordinates"
            )

    if not skipped_by_identity:
        return RequiredLifecycleProjection(plan=plan, skipped=())

    skipped_identities = frozenset(skipped_by_identity)
    actions = tuple(
        action
        for action in plan.actions
        if (action.target.chart, action.target.profile) not in skipped_identities
    )
    skipped_lifecycles = tuple(skipped_by_identity.values())
    warning = SKIPPED_REQUIRES_WARNING_PREFIX + ", ".join(
        f"{identity.chart}:{identity.profile}" for identity in skipped_lifecycles
    )
    return RequiredLifecycleProjection(
        plan=replace(
            plan,
            actions=actions,
            warnings=(*plan.warnings, warning),
        ),
        skipped=skipped_lifecycles,
    )
