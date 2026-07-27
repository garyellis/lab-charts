from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chart_manager.services.lifecycle.cluster_executor import (
    ClusterActionExecutor,
    ClusterPlanError,
)
from chart_manager.services.lifecycle.evidence import (
    ClusterIdentity,
    LocalEvidenceRepository,
)
from chart_manager.services.lifecycle.models import (
    ActionKind,
    ActionTarget,
    EdgeKind,
    LifecycleAction,
    LifecycleEdge,
    LifecyclePlan,
    Workflow,
)

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
            workflow=Workflow.CLUSTER_TEST,
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


def edge(source: LifecycleAction, target: LifecycleAction) -> LifecycleEdge:
    return LifecycleEdge(source.action_id, target.action_id, EdgeKind.RUNTIME_REQUIREMENT)


def plan(
    actions: tuple[LifecycleAction, ...],
    edges: tuple[LifecycleEdge, ...],
    *,
    workflow: Workflow = Workflow.CLUSTER_TEST,
) -> LifecyclePlan:
    return LifecyclePlan(
        workflow=workflow,
        chart="grafana",
        profile="smoke",
        actions=actions,
        edges=edges,
    )


def executor(
    calls: list[str],
    *,
    helm: FakeHelm | None = None,
    repository: LocalEvidenceRepository | None = None,
) -> ClusterActionExecutor:
    return ClusterActionExecutor(
        helm=helm or FakeHelm(calls),
        kubectl=FakeKubectl(calls),
        repository=repository,
        clock=lambda: NOW,
    )


def test_executes_cluster_actions_in_deterministic_dependency_order() -> None:
    namespace = action("cluster:grafana:namespace", ActionKind.NAMESPACE_ENSURE)
    dependency = action("cluster:grafana:dependency", ActionKind.HELM_DEPENDENCY_UPDATE)
    install = action("cluster:grafana:install", ActionKind.HELM_UPGRADE_INSTALL)
    ready = action("cluster:grafana:ready", ActionKind.WORKLOAD_READY)
    helm_test = action("cluster:grafana:test", ActionKind.HELM_TEST)
    lifecycle_plan = plan(
        # Intentionally not topologically sorted.
        (install, helm_test, dependency, namespace, ready),
        (
            edge(namespace, install),
            edge(dependency, install),
            edge(install, ready),
            edge(ready, helm_test),
        ),
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


def test_diamond_graph_executes_shared_prerequisite_once() -> None:
    shared = action("cluster:shared:dependency", ActionKind.HELM_DEPENDENCY_UPDATE)
    left = action("cluster:left:namespace", ActionKind.NAMESPACE_ENSURE, namespace="left")
    right = action("cluster:right:namespace", ActionKind.NAMESPACE_ENSURE, namespace="right")
    final = action("cluster:final:install", ActionKind.HELM_UPGRADE_INSTALL)
    lifecycle_plan = plan(
        (shared, left, right, final),
        (
            edge(shared, left),
            edge(shared, right),
            edge(left, final),
            edge(right, final),
        ),
    )
    calls: list[str] = []

    result = executor(calls).execute(lifecycle_plan)

    assert result.ok
    assert calls.count("dependency:grafana") == 1
    assert len(result.outcomes) == 4
    assert len({outcome.action_id for outcome in result.outcomes}) == 4


def test_failure_skips_only_downstream_actions_and_keeps_complete_outcomes() -> None:
    dependency = action("cluster:grafana:dependency", ActionKind.HELM_DEPENDENCY_UPDATE)
    namespace = action("cluster:grafana:namespace", ActionKind.NAMESPACE_ENSURE)
    install = action("cluster:grafana:install", ActionKind.HELM_UPGRADE_INSTALL)
    ready = action("cluster:grafana:ready", ActionKind.WORKLOAD_READY)
    lifecycle_plan = plan(
        (dependency, namespace, install, ready),
        (edge(dependency, install), edge(namespace, install), edge(install, ready)),
    )
    calls: list[str] = []
    helm = FakeHelm(calls, fail_dependency=True)

    result = executor(calls, helm=helm).execute(lifecycle_plan)

    assert not result.ok
    assert [outcome.verdict for outcome in result.outcomes] == [
        "FAIL",
        "PASS",
        "SKIP",
        "SKIP",
    ]
    assert result.outcomes[2].reason == "PrerequisiteFailed"
    assert result.outcomes[3].reason == "PrerequisiteFailed"
    assert calls == ["dependency:grafana", "namespace:monitoring"]


def test_fail_fast_marks_remaining_independent_work_skipped() -> None:
    dependency = action("cluster:grafana:dependency", ActionKind.HELM_DEPENDENCY_UPDATE)
    namespace = action("cluster:grafana:namespace", ActionKind.NAMESPACE_ENSURE)
    lifecycle_plan = plan((dependency, namespace), ())
    calls: list[str] = []

    result = executor(
        calls,
        helm=FakeHelm(calls, fail_dependency=True),
    ).execute(lifecycle_plan, fail_fast=True)

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

    result = executor(calls, helm=helm).execute(plan((helm_test,), ()))

    assert not result.ok
    assert result.outcomes[0].verdict == "FAIL"
    assert result.outcomes[0].reason == "ActionFailed"
    assert result.outcomes[0].detail == "helm test exited 1: pod assertion failed"


def test_records_evidence_for_executed_and_skipped_actions(tmp_path: Path) -> None:
    dependency = action("cluster:grafana:dependency", ActionKind.HELM_DEPENDENCY_UPDATE)
    install = action("cluster:grafana:install", ActionKind.HELM_UPGRADE_INSTALL)
    lifecycle_plan = plan((dependency, install), (edge(dependency, install),))
    calls: list[str] = []
    repository = LocalEvidenceRepository(tmp_path / "state")
    cluster = ClusterIdentity(name="chart-manager", context="kind-chart-manager", uid="node-1")

    result = executor(
        calls,
        helm=FakeHelm(calls, fail_dependency=True),
        repository=repository,
    ).execute(
        lifecycle_plan,
        run_id="cluster-run-1",
        cluster=cluster,
    )
    history = repository.history()

    assert len(result.evidence_paths) == 2
    assert result.diagnostics == ()
    assert {record.verdict for record in history.records} == {"FAIL", "SKIP"}
    assert {record.input_digest for record in history.records} == {
        dependency.input_digest,
        install.input_digest,
    }
    assert all(record.cluster == cluster for record in history.records)
    assert all(record.target.workflow == "cluster-test" for record in history.records)


def test_rejects_validation_plan_before_calling_cluster_integrations() -> None:
    render = action("validation:grafana:dev:render", ActionKind.RENDER)
    calls: list[str] = []

    with pytest.raises(ClusterPlanError, match="requires workflow"):
        executor(calls).execute(plan((render,), (), workflow=Workflow.VALIDATION))

    assert calls == []


def test_rejects_empty_cluster_plan_instead_of_reporting_vacuous_success() -> None:
    calls: list[str] = []

    with pytest.raises(ClusterPlanError, match="contains no actions"):
        executor(calls).execute(plan((), ()))

    assert calls == []
