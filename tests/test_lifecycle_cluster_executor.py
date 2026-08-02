from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chart_manager.services.lifecycle.cluster_executor import (
    ClusterActionExecutor,
    ClusterPlanError,
)
from chart_manager.services.lifecycle.models import (
    ActionKind,
    ActionTarget,
    LifecycleAction,
    LifecyclePlan,
)
from chart_manager.services.progress import ProgressEvent

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
) -> ClusterActionExecutor:
    return ClusterActionExecutor(
        helm=helm or FakeHelm(calls),
        kubectl=FakeKubectl(calls),
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
