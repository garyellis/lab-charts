from pathlib import Path

import pytest

from chart_manager.services.lifecycle.models import (
    ActionKind,
    ActionTarget,
    LifecycleAction,
    LifecyclePlan,
)
from chart_manager.services.lifecycle.plan_projection import (
    EXTERNAL_BOOTSTRAP_WARNING_PREFIX,
    ExternallySatisfiedLifecycle,
    exclude_bootstrap_owned_charts,
)


def action(chart: str, suffix: str, kind: ActionKind) -> LifecycleAction:
    return LifecycleAction(
        action_id=f"cluster-test:{chart}:minimal:{suffix}",
        kind=kind,
        target=ActionTarget(
            chart=chart,
            profile="minimal",
            release=chart,
            namespace="monitoring",
        ),
        input_digest=f"digest-{chart}-{suffix}",
        chart_path=Path("charts") / chart,
    )


def cluster_plan() -> LifecyclePlan:
    cilium_namespace = action("cilium", "namespace", ActionKind.NAMESPACE_ENSURE)
    cilium_install = action("cilium", "install", ActionKind.HELM_UPGRADE_INSTALL)
    cilium_test = action("cilium", "test", ActionKind.HELM_TEST)
    grafana_namespace = action("grafana", "namespace", ActionKind.NAMESPACE_ENSURE)
    grafana_dependency = action(
        "grafana", "dependency", ActionKind.HELM_DEPENDENCY_UPDATE
    )
    grafana_install = action("grafana", "install", ActionKind.HELM_UPGRADE_INSTALL)
    return LifecyclePlan(
        chart="grafana",
        profile="minimal",
        actions=(
            cilium_namespace,
            grafana_namespace,
            cilium_install,
            grafana_dependency,
            cilium_test,
            grafana_install,
        ),
        warnings=("authored warning",),
    )


def externally_satisfied(
    chart: str,
    *,
    profile: str = "minimal",
    namespace: str = "monitoring",
    chart_path: Path | None = None,
) -> ExternallySatisfiedLifecycle:
    return ExternallySatisfiedLifecycle(
        chart_path=(chart_path or Path("charts") / chart).resolve(),
        chart=chart,
        profile=profile,
        namespace=namespace,
    )


def test_removes_bootstrap_chart_actions() -> None:
    original = cluster_plan()

    projected = exclude_bootstrap_owned_charts(
        original,
        {externally_satisfied("cilium")},
    )

    assert [item.action_id for item in projected.actions] == [
        "cluster-test:grafana:minimal:namespace",
        "cluster-test:grafana:minimal:dependency",
        "cluster-test:grafana:minimal:install",
    ]
    assert projected.warnings == (
        "authored warning",
        EXTERNAL_BOOTSTRAP_WARNING_PREFIX
        + "cilium; environment-owned preparation/install actions were excluded "
        "from this executable plan",
    )
    # Projection is pure: the compiler-owned input remains untouched.
    assert len(original.actions) == 6


def test_preserves_relative_order_of_remaining_actions() -> None:
    original = cluster_plan()
    original_action_ids = [item.action_id for item in original.actions]

    projected = exclude_bootstrap_owned_charts(
        original,
        (externally_satisfied("cilium"),),
    )

    assert [item.action_id for item in projected.actions] == [
        item for item in original_action_ids if ":cilium:" not in item
    ]


def test_absent_bootstrap_chart_is_an_idempotent_noop() -> None:
    original = cluster_plan()

    projected = exclude_bootstrap_owned_charts(
        original,
        {externally_satisfied("not-in-plan")},
    )

    assert projected is original


def test_bootstrap_requested_target_keeps_only_readiness_and_test_actions() -> None:
    original = cluster_plan()
    projected = exclude_bootstrap_owned_charts(
        original,
        {
            externally_satisfied("grafana"),
            externally_satisfied("cilium"),
        },
    )

    # This fixture has no target readiness/test actions, so all target install
    # preparation is removed. A compiled plan retains its readiness/test pair.
    assert projected.actions == ()


@pytest.mark.parametrize(
    "identity",
    [
        externally_satisfied("cilium", profile="full"),
        externally_satisfied("cilium", namespace="kube-system"),
        externally_satisfied("cilium", chart_path=Path("elsewhere/cilium")),
    ],
)
def test_requires_exact_managed_lifecycle_identity(
    identity: ExternallySatisfiedLifecycle,
) -> None:
    original = cluster_plan()

    projected = exclude_bootstrap_owned_charts(original, {identity})

    assert projected is original
