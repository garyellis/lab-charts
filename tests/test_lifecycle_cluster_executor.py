from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chart_manager.services.lifecycle.cluster_executor import (
    CLEANUP_SKIP_REASON,
    ClusterActionExecutor,
    ClusterPlanError,
)
from chart_manager.services.lifecycle.models import (
    ActionKind,
    ActionTarget,
    LifecycleAction,
    LifecyclePlan,
)
from chart_manager.shared.cluster.progress import ProgressEvent

NOW = datetime(2026, 7, 27, 9, tzinfo=UTC)


@dataclass(frozen=True)
class FakeHelmTestResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeHelm:
    def __init__(
        self,
        calls: list[str],
        *,
        fail_dependency: bool = False,
        test_result: FakeHelmTestResult | None = None,
    ) -> None:
        self.calls = calls
        self.fail_dependency = fail_dependency
        self.test_result = test_result or FakeHelmTestResult()

    def dependency_update_if_stale(self, chart_path: Path) -> None:
        self.calls.append(f"dependency:{chart_path.name}")
        if self.fail_dependency:
            raise RuntimeError("dependency update failed")

    def lint(self, chart_path: Path, values: list[Path] | None = None) -> None:
        self.calls.append(f"lint:{chart_path.name}:{len(values or [])}")

    def upgrade_install(
        self,
        release: str,
        chart_path: Path,
        *,
        namespace: str,
        values: list[Path] | None,
        timeout: str,
        wait: bool,
    ) -> None:
        self.calls.append(f"install:{release}:{namespace}:{timeout}:{wait}:{len(values)}")

    def test(
        self,
        release: str,
        *,
        namespace: str,
        timeout: str | None,
    ) -> FakeHelmTestResult:
        self.calls.append(f"test:{release}:{namespace}:{timeout}")
        return self.test_result


class FakeKubectl:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def create_namespace(self, namespace: str) -> None:
        self.calls.append(f"namespace:{namespace}")

    def wait_workloads_ready(
        self,
        namespace: str,
        timeout: str = "10m",
        *,
        selector: str | None = None,
    ) -> None:
        self.calls.append(f"ready:{namespace}:{timeout}:{selector}")


class FakeHooks:
    def __init__(self, calls: list[str], *, fail_on: ActionKind | None = None) -> None:
        self.calls = calls
        self.fail_on = fail_on

    def run(self, action: LifecycleAction) -> None:
        self.calls.append(f"hook:{action.kind.value}:{' '.join(action.command)}")
        if action.kind is self.fail_on:
            raise RuntimeError("pre-install hook exited 3: scripts/hook")


def action(
    action_id: str,
    kind: ActionKind,
    *,
    chart: str = "grafana",
    namespace: str = "monitoring",
) -> LifecycleAction:
    return LifecycleAction(
        action_id=action_id,
        kind=kind,
        target=ActionTarget(
            chart=chart,
            profile="smoke",
            release=chart,
            namespace=namespace,
        ),
        input_digest=f"digest-{action_id}",
        chart_path=Path("charts") / chart,
        values=(Path("values-smoke.yaml"),),
        timeout="10m",
        command=("scripts/hook", kind.value) if kind.value.startswith("hook-") else (),
    )


def plan(actions: tuple[LifecycleAction, ...]) -> LifecyclePlan:
    return LifecyclePlan(
        chart="grafana",
        profile="smoke",
        actions=actions,
    )


def executor(
    calls: list[str],
    *,
    helm: FakeHelm | None = None,
    hooks: FakeHooks | None = None,
) -> ClusterActionExecutor:
    return ClusterActionExecutor(
        helm=helm or FakeHelm(calls),
        kubectl=FakeKubectl(calls),
        hooks=hooks or FakeHooks(calls),
        clock=lambda: NOW,
    )


def test_executes_cluster_actions_in_authoritative_plan_order() -> None:
    namespace = action("cluster:grafana:namespace", ActionKind.NAMESPACE_ENSURE)
    dependency = action("cluster:grafana:dependency", ActionKind.HELM_DEPENDENCY_UPDATE)
    install = action("cluster:grafana:install", ActionKind.HELM_UPGRADE_INSTALL)
    ready = action("cluster:grafana:ready", ActionKind.WORKLOAD_READY)
    helm_test = action("cluster:grafana:test", ActionKind.HELM_TEST)
    lifecycle_plan = plan(
        (dependency, namespace, install, ready, helm_test),
    )
    calls: list[str] = []

    result = executor(calls).execute(lifecycle_plan)

    assert result.ok
    assert [outcome.action_id for outcome in result.outcomes] == [
        dependency.action_id,
        namespace.action_id,
        install.action_id,
        ready.action_id,
        helm_test.action_id,
    ]
    assert calls == [
        "dependency:grafana",
        "namespace:monitoring",
        "install:grafana:monitoring:10m:False:1",
        "ready:monitoring:10m:app.kubernetes.io/instance=grafana",
        "test:grafana:monitoring:10m",
    ]


def test_each_ordered_action_executes_once() -> None:
    shared = action("cluster:shared:dependency", ActionKind.HELM_DEPENDENCY_UPDATE)
    left = action("cluster:left:namespace", ActionKind.NAMESPACE_ENSURE, namespace="left")
    right = action("cluster:right:namespace", ActionKind.NAMESPACE_ENSURE, namespace="right")
    final = action("cluster:final:install", ActionKind.HELM_UPGRADE_INSTALL)
    lifecycle_plan = plan((shared, left, right, final))
    calls: list[str] = []

    result = executor(calls).execute(lifecycle_plan)

    assert result.ok
    assert calls.count("dependency:grafana") == 1
    assert len(result.outcomes) == 4
    assert len({outcome.action_id for outcome in result.outcomes}) == 4


def test_failure_skips_every_later_action_and_keeps_complete_outcomes() -> None:
    dependency = action("cluster:grafana:dependency", ActionKind.HELM_DEPENDENCY_UPDATE)
    namespace = action("cluster:grafana:namespace", ActionKind.NAMESPACE_ENSURE)
    install = action("cluster:grafana:install", ActionKind.HELM_UPGRADE_INSTALL)
    ready = action("cluster:grafana:ready", ActionKind.WORKLOAD_READY)
    lifecycle_plan = plan((dependency, namespace, install, ready))
    calls: list[str] = []
    helm = FakeHelm(calls, fail_dependency=True)

    result = executor(calls, helm=helm).execute(lifecycle_plan)

    assert not result.ok
    assert [outcome.verdict for outcome in result.outcomes] == [
        "FAIL",
        "SKIP",
        "SKIP",
        "SKIP",
    ]
    assert all(outcome.reason == "FailFast" for outcome in result.outcomes[1:])
    assert calls == ["dependency:grafana"]


def test_emits_ordered_progress_for_completion_failure_and_skip() -> None:
    dependency = action("cluster:grafana:dependency", ActionKind.HELM_DEPENDENCY_UPDATE)
    install = action("cluster:grafana:install", ActionKind.HELM_UPGRADE_INSTALL)
    events: list[ProgressEvent] = []
    calls: list[str] = []
    lifecycle_plan = plan((dependency, install))

    result = ClusterActionExecutor(
        helm=FakeHelm(calls, fail_dependency=True),
        kubectl=FakeKubectl(calls),
        clock=lambda: NOW,
        progress=events.append,
    ).execute(lifecycle_plan)

    assert [outcome.verdict for outcome in result.outcomes] == ["FAIL", "SKIP"]
    assert [
        (event.severity, event.label, event.message) for event in events
    ] == [
        ("step", "Updating dependencies", "grafana:smoke in monitoring"),
        (
            "error",
            "Failed",
            "grafana:smoke in monitoring: dependency update failed",
        ),
        ("detail", "Skipped", "grafana:smoke in monitoring"),
    ]


def test_emits_start_then_completion_for_each_successful_action() -> None:
    namespace = action("cluster:grafana:namespace", ActionKind.NAMESPACE_ENSURE)
    lint = action("cluster:grafana:lint", ActionKind.HELM_LINT)
    events: list[ProgressEvent] = []
    calls: list[str] = []

    ClusterActionExecutor(
        helm=FakeHelm(calls),
        kubectl=FakeKubectl(calls),
        clock=lambda: NOW,
        progress=events.append,
    ).execute(plan((namespace, lint)))

    assert [(event.label, event.message) for event in events] == [
        ("Ensuring namespace", "grafana:smoke in monitoring"),
        ("Completed", "grafana:smoke in monitoring"),
        ("Linting", "grafana:smoke in monitoring"),
        ("Completed", "grafana:smoke in monitoring"),
    ]


def test_fail_fast_is_unconditional() -> None:
    dependency = action("cluster:grafana:dependency", ActionKind.HELM_DEPENDENCY_UPDATE)
    namespace = action("cluster:grafana:namespace", ActionKind.NAMESPACE_ENSURE)
    lifecycle_plan = plan((dependency, namespace))
    calls: list[str] = []

    result = executor(
        calls,
        helm=FakeHelm(calls, fail_dependency=True),
    ).execute(lifecycle_plan)

    assert [outcome.verdict for outcome in result.outcomes] == ["FAIL", "SKIP"]
    assert result.outcomes[1].reason == "FailFast"
    assert calls == ["dependency:grafana"]


def test_nonzero_helm_test_is_a_failed_terminal_outcome() -> None:
    helm_test = action("cluster:grafana:test", ActionKind.HELM_TEST)
    calls: list[str] = []
    helm = FakeHelm(
        calls,
        test_result=FakeHelmTestResult(returncode=1, stderr="pod assertion failed"),
    )

    result = executor(calls, helm=helm).execute(plan((helm_test,)))

    assert not result.ok
    assert result.outcomes[0].verdict == "FAIL"
    assert result.outcomes[0].reason == "ActionFailed"
    assert result.outcomes[0].detail == "helm test exited 1: pod assertion failed"


def test_executes_lint_with_selected_values() -> None:
    lint = action("cluster:grafana:lint", ActionKind.HELM_LINT)
    calls: list[str] = []

    result = executor(calls).execute(plan((lint,)))

    assert result.ok
    assert calls == ["lint:grafana:1"]


def test_rejects_empty_cluster_plan_instead_of_reporting_vacuous_success() -> None:
    calls: list[str] = []

    with pytest.raises(ClusterPlanError, match="contains no actions"):
        executor(calls).execute(plan(()))

    assert calls == []


def test_rejects_duplicate_action_ids_before_calling_integrations() -> None:
    duplicate = action("cluster:grafana:namespace", ActionKind.NAMESPACE_ENSURE)
    calls: list[str] = []

    with pytest.raises(ClusterPlanError, match="duplicate action id"):
        executor(calls).execute(plan((duplicate, duplicate)))

    assert calls == []


# --- cluster-test hooks ------------------------------------------------------


def _hooked_plan() -> LifecyclePlan:
    return plan(
        (
            action("cluster:grafana:pre", ActionKind.HOOK_PRE_INSTALL),
            action("cluster:grafana:install", ActionKind.HELM_UPGRADE_INSTALL),
            action("cluster:grafana:post", ActionKind.HOOK_POST_INSTALL),
            action("cluster:grafana:test", ActionKind.HELM_TEST),
            action("cluster:grafana:cleanup", ActionKind.HOOK_CLEANUP),
        )
    )


def test_install_hooks_run_through_the_hooks_port_and_cleanups_are_skipped() -> None:
    calls: list[str] = []

    result = executor(calls).execute(_hooked_plan())

    assert calls == [
        "hook:hook-pre-install:scripts/hook hook-pre-install",
        "install:grafana:monitoring:10m:False:1",
        "hook:hook-post-install:scripts/hook hook-post-install",
        "test:grafana:monitoring:10m",
    ]
    assert [(outcome.verdict, outcome.reason) for outcome in result.outcomes] == [
        ("PASS", "ActionCompleted"),
        ("PASS", "ActionCompleted"),
        ("PASS", "ActionCompleted"),
        ("PASS", "ActionCompleted"),
        ("SKIP", CLEANUP_SKIP_REASON),
    ]
    assert result.ok


def test_failed_hook_fails_fast_and_every_action_keeps_exactly_one_outcome() -> None:
    calls: list[str] = []
    hooks = FakeHooks(calls, fail_on=ActionKind.HOOK_PRE_INSTALL)
    lifecycle_plan = _hooked_plan()

    result = executor(calls, hooks=hooks).execute(lifecycle_plan)

    assert not result.ok
    assert [outcome.action_id for outcome in result.outcomes] == [
        planned.action_id for planned in lifecycle_plan.actions
    ]
    assert [(outcome.verdict, outcome.reason) for outcome in result.outcomes] == [
        ("FAIL", "ActionFailed"),
        ("SKIP", "FailFast"),
        ("SKIP", "FailFast"),
        ("SKIP", "FailFast"),
        ("SKIP", CLEANUP_SKIP_REASON),
    ]
    assert result.outcomes[0].detail == "pre-install hook exited 3: scripts/hook"
    assert calls == ["hook:hook-pre-install:scripts/hook hook-pre-install"]


def test_rejects_a_hook_plan_without_a_hooks_port_before_calling_integrations() -> None:
    calls: list[str] = []

    with pytest.raises(ClusterPlanError, match="hooks port"):
        ClusterActionExecutor(
            helm=FakeHelm(calls),
            kubectl=FakeKubectl(calls),
            clock=lambda: NOW,
        ).execute(_hooked_plan())

    assert calls == []


def test_rejects_a_cleanup_outside_the_plan_tail_before_calling_integrations() -> None:
    cleanup = action("cluster:grafana:cleanup", ActionKind.HOOK_CLEANUP)
    install = action("cluster:grafana:install", ActionKind.HELM_UPGRADE_INSTALL)
    calls: list[str] = []

    with pytest.raises(ClusterPlanError, match="contiguous tail"):
        executor(calls).execute(plan((cleanup, install)))

    assert calls == []


def test_execute_cleanups_continues_past_a_failure() -> None:
    calls: list[str] = []
    hooks = FakeHooks(calls, fail_on=ActionKind.HOOK_CLEANUP)
    cleanups = plan(
        (
            action("cluster:grafana:cleanup", ActionKind.HOOK_CLEANUP),
            action("cluster:loki:cleanup", ActionKind.HOOK_CLEANUP, chart="loki"),
        )
    )

    result = executor(calls, hooks=hooks).execute_cleanups(cleanups)

    assert len(calls) == 2
    assert [outcome.verdict for outcome in result.outcomes] == ["FAIL", "FAIL"]
    assert not result.ok


def test_execute_cleanups_rejects_any_other_action() -> None:
    calls: list[str] = []

    with pytest.raises(ClusterPlanError, match="hook-pre-install"):
        executor(calls).execute_cleanups(_hooked_plan())

    assert calls == []
