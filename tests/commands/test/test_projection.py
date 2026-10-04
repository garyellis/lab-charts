from pathlib import Path

import pytest

from chart_manager.commands.test.models import (
    ActionKind,
    ActionTarget,
    LifecycleAction,
    LifecyclePlan,
)
from chart_manager.commands.test.plan import (
    EXTERNAL_BOOTSTRAP_WARNING_PREFIX,
    SKIPPED_REQUIRES_WARNING_PREFIX,
    cleanup_tail,
    exclude_bootstrap_owned_charts,
    exclude_required_lifecycles,
)
from chart_manager.shared.cluster.bootstrap import ExternallySatisfiedLifecycle


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
    cilium_install = action("cilium", "install", ActionKind.INSTALL)
    cilium_test = action("cilium", "test", ActionKind.HELM_TEST)
    grafana_namespace = action("grafana", "namespace", ActionKind.NAMESPACE_ENSURE)
    grafana_dependency = action("grafana", "dependency", ActionKind.HELM_LINT)
    grafana_install = action("grafana", "install", ActionKind.INSTALL)
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
        frozenset({externally_satisfied("cilium")}),
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
        original, frozenset({externally_satisfied("cilium")})
    )

    assert [item.action_id for item in projected.actions] == [
        item for item in original_action_ids if ":cilium:" not in item
    ]


def test_absent_bootstrap_chart_is_an_idempotent_noop() -> None:
    original = cluster_plan()

    projected = exclude_bootstrap_owned_charts(
        original,
        frozenset({externally_satisfied("not-in-plan")}),
    )

    assert projected is original


def test_a_bootstrap_owned_target_keeps_a_readiness_wait_instead_of_its_install() -> None:
    projected = exclude_bootstrap_owned_charts(
        cluster_plan(),
        frozenset({externally_satisfied("grafana"), externally_satisfied("cilium")}),
    )

    assert [(a.target.chart, a.kind) for a in projected.actions] == [
        ("grafana", ActionKind.WORKLOAD_READY)
    ]
    assert projected.actions[0].action_id == "cluster-test.grafana.minimal.workload-ready"


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

    projected = exclude_bootstrap_owned_charts(original, frozenset({identity}))

    assert projected is original


def test_skip_requires_removes_every_required_action_including_tests() -> None:
    base_install = action("base", "install", ActionKind.INSTALL)
    base_test = action("base", "test", ActionKind.HELM_TEST)
    dependency_ready = action("dependency", "ready", ActionKind.INSTALL)
    dependency_test = action("dependency", "test", ActionKind.HELM_TEST)
    target_install = action("app", "install", ActionKind.INSTALL)
    target_test = action("app", "test", ActionKind.HELM_TEST)
    original = LifecyclePlan(
        chart="app",
        profile="minimal",
        actions=(
            base_install,
            base_test,
            dependency_ready,
            dependency_test,
            target_install,
            target_test,
        ),
        warnings=("authored warning",),
    )

    projected = exclude_required_lifecycles(original, [("app", "minimal")])

    assert projected.plan.actions == (target_install, target_test)
    assert [(item.chart, item.profile) for item in projected.skipped] == [
        ("base", "minimal"),
        ("dependency", "minimal"),
    ]
    assert projected.plan.warnings == (
        "authored warning",
        SKIPPED_REQUIRES_WARNING_PREFIX + "base:minimal, dependency:minimal",
    )
    assert original.actions[1].kind is ActionKind.HELM_TEST


def test_skip_requires_preserves_every_explicit_fanout_target() -> None:
    shared_test = action("shared", "test", ActionKind.HELM_TEST)
    app_test = action("app", "test", ActionKind.HELM_TEST)
    dependent_test = action("dependent", "test", ActionKind.HELM_TEST)
    original = LifecyclePlan(
        chart="app",
        profile="minimal",
        actions=(shared_test, app_test, dependent_test),
    )

    projected = exclude_required_lifecycles(
        original,
        [("app", "minimal"), ("dependent", "minimal")],
    )

    assert projected.plan.actions == (app_test, dependent_test)
    assert [item.chart for item in projected.skipped] == ["shared"]


def test_cleanup_tail_moves_cleanups_last_in_reverse_entry_order() -> None:
    base_install = action("base", "install", ActionKind.INSTALL)
    base_cleanup = action("base", "cleanup", ActionKind.HOOK_CLEANUP)
    app_install = action("app", "install", ActionKind.INSTALL)
    app_cleanup = action("app", "cleanup", ActionKind.HOOK_CLEANUP)
    web_install = action("web", "install", ActionKind.INSTALL)
    web_cleanup = action("web", "cleanup", ActionKind.HOOK_CLEANUP)

    reordered = cleanup_tail(
        [
            base_install,
            base_cleanup,
            app_install,
            web_cleanup,
            web_install,
            app_cleanup,
        ]
    )

    assert reordered == (
        base_install,
        app_install,
        web_install,
        web_cleanup,
        app_cleanup,
        base_cleanup,
    )


def test_cleanup_tail_without_cleanups_keeps_order() -> None:
    actions = cluster_plan().actions

    assert cleanup_tail(actions) == actions
