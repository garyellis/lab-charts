from pathlib import Path

import pytest

from chart_manager.services.lifecycle.models import (
    ActionKind,
    ActionTarget,
    EdgeKind,
    LifecycleAction,
    LifecycleEdge,
    LifecyclePlan,
    Workflow,
)
from chart_manager.services.lifecycle.plan_projection import (
    EXTERNAL_BOOTSTRAP_WARNING_PREFIX,
    PlanProjectionError,
    exclude_bootstrap_owned_charts,
)


def action(chart: str, suffix: str, kind: ActionKind) -> LifecycleAction:
    return LifecycleAction(
        action_id=f"cluster-test:{chart}:minimal:{suffix}",
        kind=kind,
        target=ActionTarget(
            workflow=Workflow.CLUSTER_TEST,
            chart=chart,
            profile="minimal",
            release=chart,
            namespace="monitoring",
        ),
        input_digest=f"digest-{chart}-{suffix}",
        chart_path=Path("charts") / chart,
    )


def edge(
    source: LifecycleAction,
    target: LifecycleAction,
    kind: EdgeKind = EdgeKind.SEQUENCE,
) -> LifecycleEdge:
    return LifecycleEdge(source.action_id, target.action_id, kind)


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
        workflow=Workflow.CLUSTER_TEST,
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
        edges=(
            edge(cilium_namespace, cilium_install),
            edge(cilium_install, cilium_test),
            edge(grafana_namespace, grafana_install),
            edge(grafana_dependency, grafana_install),
            edge(cilium_test, grafana_install, EdgeKind.RUNTIME_REQUIREMENT),
        ),
        warnings=("authored warning",),
    )


def test_removes_bootstrap_chart_actions_and_incident_runtime_edges() -> None:
    original = cluster_plan()

    projected = exclude_bootstrap_owned_charts(original, {"cilium"})

    assert [item.action_id for item in projected.actions] == [
        "cluster-test:grafana:minimal:namespace",
        "cluster-test:grafana:minimal:dependency",
        "cluster-test:grafana:minimal:install",
    ]
    assert [(item.source, item.target) for item in projected.edges] == [
        (
            "cluster-test:grafana:minimal:namespace",
            "cluster-test:grafana:minimal:install",
        ),
        (
            "cluster-test:grafana:minimal:dependency",
            "cluster-test:grafana:minimal:install",
        ),
    ]
    remaining_ids = {item.action_id for item in projected.actions}
    assert all(
        item.source in remaining_ids and item.target in remaining_ids
        for item in projected.edges
    )
    assert projected.warnings == (
        "authored warning",
        EXTERNAL_BOOTSTRAP_WARNING_PREFIX
        + "cilium; actions and incident edges were excluded from this executable plan",
    )
    # Projection is pure: the compiler-owned input remains untouched.
    assert len(original.actions) == 6
    assert len(original.edges) == 5


def test_preserves_relative_order_of_remaining_actions_and_edges() -> None:
    original = cluster_plan()
    original_action_ids = [item.action_id for item in original.actions]
    original_edges = list(original.edges)

    projected = exclude_bootstrap_owned_charts(original, ("cilium",))

    assert [item.action_id for item in projected.actions] == [
        item for item in original_action_ids if ":cilium:" not in item
    ]
    assert list(projected.edges) == [
        item
        for item in original_edges
        if ":cilium:" not in item.source and ":cilium:" not in item.target
    ]


def test_absent_bootstrap_chart_is_an_idempotent_noop() -> None:
    original = cluster_plan()

    projected = exclude_bootstrap_owned_charts(original, {"not-in-plan"})

    assert projected is original


def test_rejects_excluding_the_requested_target_chart() -> None:
    with pytest.raises(PlanProjectionError, match="requested target chart 'grafana'"):
        exclude_bootstrap_owned_charts(cluster_plan(), {"grafana", "cilium"})


def test_rejects_projection_of_validation_plan() -> None:
    render = action("grafana", "render", ActionKind.RENDER)
    validation = LifecyclePlan(
        workflow=Workflow.VALIDATION,
        chart="grafana",
        environment="dev",
        actions=(render,),
        edges=(),
    )

    with pytest.raises(PlanProjectionError, match="requires a cluster-test plan"):
        exclude_bootstrap_owned_charts(validation, {"cilium"})
