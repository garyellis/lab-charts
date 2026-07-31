from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.chart_config import (
    load_chart_lifecycle,
    require_cluster_test,
)
from chart_manager.services.clusters.bootstrap import LocalBootstrapExecutor
from chart_manager.services.clusters.ephemeral import (
    EphemeralTestClusterService,
    EphemeralTestRequest,
)
from chart_manager.services.domain.cluster_tests import (
    ClusterTestProfile,
    ClusterTestSpec,
    SpecError,
)
from chart_manager.services.lifecycle.models import (
    ActionKind,
    ActionTarget,
    LifecycleAction,
    LifecyclePlan,
    Workflow,
)
from chart_manager.services.lifecycle.plan_projection import ExternallySatisfiedLifecycle

from .conftest import cli


def _alloy_spec() -> ClusterTestSpec:
    lifecycle = load_chart_lifecycle(Path("charts/alloy/chart-lifecycle.yaml"))
    return require_cluster_test(lifecycle, chart_name="alloy")


def test_load_test_spec_accepts_chart_refs() -> None:
    spec = _alloy_spec()

    minimal = spec.profile("minimal")

    assert minimal.requires[0].chart == "prometheus-operator"
    assert minimal.requires[0].profile == "minimal"
    assert minimal.helm_test is True


def test_unknown_profile_raises_spec_error() -> None:
    spec = _alloy_spec()

    with pytest.raises(SpecError):
        spec.profile("missing")


def test_dependent_tests_is_the_only_authored_reverse_target_field() -> None:
    spec = ClusterTestSpec.model_validate(
        {
            "profiles": {"minimal": {}},
            "dependentTests": [{"chart": "grafana", "profile": "with-deps"}],
        }
    )

    assert [(ref.chart, ref.profile) for ref in spec.dependent_tests] == [
        ("grafana", "with-deps")
    ]

    with pytest.raises(ValidationError, match="reverseTests"):
        ClusterTestSpec.model_validate(
            {
                "profiles": {"minimal": {}},
                "reverseTests": [{"chart": "grafana"}],
            }
        )


def test_cli_exposes_dependent_tests_only_on_chart_test() -> None:
    root_help = cli("--help")
    chart_test_help = cli("charts", "test", "--help")

    assert root_help.exit_code == 0
    assert "deps" not in root_help.stdout
    assert chart_test_help.exit_code == 0
    assert "--dependent-tests" in chart_test_help.stdout
    assert "--reverse" not in chart_test_help.stdout


def test_cluster_test_profile_defaults_to_running_helm_tests() -> None:
    assert ClusterTestProfile().helm_test is True


def test_cluster_test_profile_accepts_disabled_helm_tests() -> None:
    assert ClusterTestProfile(helmTest=False).helm_test is False


def test_cluster_test_profile_rejects_removed_checks_configuration() -> None:
    with pytest.raises(ValidationError, match="checks"):
        ClusterTestProfile.model_validate(
            {"checks": [{"name": "pods-ready", "type": "helm-test"}]}
        )


# ----- lifecycle-backed ephemeral execution --------------------------------


class _MigrationKind:
    def ensure_cluster(self, _name: str, *, config: Path | None = None) -> None:
        pass

    def control_plane_ip(self, _name: str) -> str:
        return "172.18.0.2"


class _MigrationKubectl:
    context = "kind-configured"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.diagnostic_namespaces: list[str] = []

    def wait_apiserver_ready(self) -> None:
        self.calls.append("apiserver")

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

    def diagnostics(self, namespace: str) -> str:
        self.diagnostic_namespaces.append(namespace)
        return "pod diagnostics"


class _MigrationHelm:
    def __init__(
        self,
        calls: list[str],
        *,
        fail_dependency: bool = False,
        fail_lint: bool = False,
    ) -> None:
        self.calls = calls
        self.fail_dependency = fail_dependency
        self.fail_lint = fail_lint

    def dependency_update_if_stale(self, chart_path: Path) -> bool:
        self.calls.append(f"dependency:{chart_path.name}")
        if self.fail_dependency:
            raise RuntimeError("dependency update failed")
        return True

    def upgrade_install(
        self,
        release: str,
        chart_path: Path,
        **kwargs: Any,
    ) -> object:
        self.calls.append(f"install:{release}:{chart_path.name}")
        return SimpleNamespace(status="applied")

    def test(self, release: str, **_kwargs: Any) -> object:
        self.calls.append(f"test:{release}")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def lint(self, chart_path: Path, *_args: Any, **_kwargs: Any) -> None:
        self.calls.append(f"lint:{chart_path.name}")
        if self.fail_lint:
            raise RuntimeError("lint found an invalid template")


def _migration_action(chart: str, suffix: str, kind: ActionKind) -> LifecycleAction:
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
        timeout="1m",
    )


def _profile_action(
    chart: str, profile: str, suffix: str, kind: ActionKind
) -> LifecycleAction:
    base = _migration_action(chart, suffix, kind)
    return LifecycleAction(
        action_id=f"cluster-test.{chart}.{profile}.{kind.value}",
        kind=kind,
        target=ActionTarget(
            workflow=Workflow.CLUSTER_TEST,
            chart=chart,
            profile=profile,
            release=chart,
            namespace="monitoring",
        ),
        input_digest=f"digest-{chart}-{profile}-{suffix}",
        chart_path=Path("charts") / chart,
        values=(Path(f"values-{profile}.yaml"),),
        timeout=base.timeout,
    )


def _fanout_plan(
    target: str,
    profile: str = "minimal",
    *,
    prerequisite: tuple[str, str] | None = None,
    lint: bool = False,
) -> LifecyclePlan:
    coordinates = (*(prerequisite or ()), target, profile)
    pairs = list(zip(coordinates[::2], coordinates[1::2], strict=True))
    actions: list[LifecycleAction] = []
    for chart, selected_profile in pairs:
        namespace = _profile_action(
            chart, selected_profile, "namespace", ActionKind.NAMESPACE_ENSURE
        )
        dependency = _profile_action(
            chart,
            selected_profile,
            "dependency",
            ActionKind.HELM_DEPENDENCY_UPDATE,
        )
        install = _profile_action(
            chart, selected_profile, "install", ActionKind.HELM_UPGRADE_INSTALL
        )
        lint_action = _profile_action(
            chart, selected_profile, "lint", ActionKind.HELM_LINT
        )
        ready = _profile_action(
            chart, selected_profile, "ready", ActionKind.WORKLOAD_READY
        )
        helm_test = _profile_action(
            chart, selected_profile, "test", ActionKind.HELM_TEST
        )
        actions.extend((namespace, dependency))
        if lint:
            actions.append(lint_action)
        actions.extend((install, ready, helm_test))
    return LifecyclePlan(
        workflow=Workflow.CLUSTER_TEST,
        chart=target,
        profile=profile,
        actions=tuple(actions),
    )


def _migration_plan(*, lint: bool = False) -> LifecyclePlan:
    namespace = _migration_action("grafana", "namespace", ActionKind.NAMESPACE_ENSURE)
    dependency = _migration_action(
        "grafana", "dependency", ActionKind.HELM_DEPENDENCY_UPDATE
    )
    lint_action = _migration_action("grafana", "lint", ActionKind.HELM_LINT)
    install = _migration_action("grafana", "install", ActionKind.HELM_UPGRADE_INSTALL)
    ready = _migration_action("grafana", "ready", ActionKind.WORKLOAD_READY)
    test = _migration_action("grafana", "test", ActionKind.HELM_TEST)
    actions = [namespace, dependency]
    if lint:
        actions.append(lint_action)
    actions.extend((install, ready, test))
    return LifecyclePlan(
        workflow=Workflow.CLUSTER_TEST,
        chart="grafana",
        profile="minimal",
        actions=tuple(actions),
    )


def _migration_service(
    tmp_path: Path,
    *,
    calls: list[str],
    fail_dependency: bool = False,
    fail_lint: bool = False,
) -> tuple[EphemeralTestClusterService, _MigrationKubectl]:
    (tmp_path / "kind-config.yaml").write_text("kind: Cluster\n", encoding="utf-8")
    local_cluster = tmp_path / ".chart-manager/local-cluster.yaml"
    local_cluster.parent.mkdir()
    local_cluster.write_text(
        """
apiVersion: local.cmg.io/v1alpha1
kind: LocalCluster
metadata: {name: default}
spec:
  cluster: {config: kind-config.yaml}
  bootstrap: {releases: []}
""".lstrip(),
        encoding="utf-8",
    )
    kubectl = _MigrationKubectl(calls)
    service = EphemeralTestClusterService(
        tmp_path,
        helm=_MigrationHelm(
            calls,
            fail_dependency=fail_dependency,
            fail_lint=fail_lint,
        ),  # type: ignore[arg-type]
        kind=_MigrationKind(),  # type: ignore[arg-type]
        kubectl=kubectl,  # type: ignore[arg-type]
    )
    return service, kubectl


def test_ephemeral_default_executes_projected_action_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, _kubectl = _migration_service(tmp_path, calls=calls)
    monkeypatch.setattr(
        service.lifecycle_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: _migration_plan(),
    )
    result = service.run(EphemeralTestRequest(chart="grafana", ensure_cluster=False))

    assert calls == [
        "namespace:monitoring",
        "dependency:grafana",
        "install:grafana:grafana",
        "ready:monitoring:1m:app.kubernetes.io/instance=grafana",
        "test:grafana",
    ]
    assert result.installed == ("grafana",)
    assert result.tested == ("grafana",)
    assert result.namespaces == ("monitoring",)


def test_ephemeral_no_ensure_binds_clients_to_selected_provider_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, kubectl = _migration_service(tmp_path, calls=calls)
    handles: list[Any] = []
    helm = service.helm

    def bind(handle: Any) -> tuple[Any, Any]:
        handles.append(handle)
        return helm, kubectl

    service._client_factory = bind  # type: ignore[assignment]
    monkeypatch.setattr(
        service.lifecycle_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: _migration_plan(),
    )
    service.run(
        EphemeralTestRequest(
            chart="grafana",
            cluster_name="selected",
            ensure_cluster=False,
        )
    )

    assert [handle.context for handle in handles] == ["kind-selected"]


def test_ephemeral_failure_records_partial_evidence_then_reports_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, kubectl = _migration_service(
        tmp_path,
        calls=calls,
        fail_dependency=True,
    )
    monkeypatch.setattr(
        service.lifecycle_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: _migration_plan(),
    )

    with pytest.raises(ChartManagerError, match="dependency update failed"):
        service.run(EphemeralTestRequest(chart="grafana", ensure_cluster=False))

    assert kubectl.diagnostic_namespaces == ["monitoring"]
    assert "install:grafana:grafana" not in calls


def test_ephemeral_lint_is_a_first_class_action_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, _kubectl = _migration_service(tmp_path, calls=calls)
    monkeypatch.setattr(
        service.lifecycle_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: _migration_plan(lint=True),
    )
    result = service.run(
        EphemeralTestRequest(
            chart="grafana",
            lint=True,
            ensure_cluster=False,
        )
    )

    assert calls == [
        "namespace:monitoring",
        "dependency:grafana",
        "lint:grafana",
        "install:grafana:grafana",
        "ready:monitoring:1m:app.kubernetes.io/instance=grafana",
        "test:grafana",
    ]
    assert result.installed == ("grafana",)


def test_ephemeral_lint_failure_keeps_diagnostics_and_terminal_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, kubectl = _migration_service(tmp_path, calls=calls, fail_lint=True)
    monkeypatch.setattr(
        service.lifecycle_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: _migration_plan(lint=True),
    )

    with pytest.raises(
        ChartManagerError,
        match=r"cluster action failed for grafana \(helm-lint\): "
        r"lint found an invalid template",
    ):
        service.run(
            EphemeralTestRequest(
                chart="grafana",
                lint=True,
                ensure_cluster=False,
            )
        )

    assert kubectl.diagnostic_namespaces == ["monitoring"]
    assert "install:grafana:grafana" not in calls


def test_ephemeral_bootstrap_target_only_runs_readiness_and_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, _kubectl = _migration_service(tmp_path, calls=calls)
    monkeypatch.setattr(
        LocalBootstrapExecutor,
        "preflight",
        lambda *_args, **_kwargs: frozenset(
            {
                ExternallySatisfiedLifecycle(
                    chart_path=(Path("charts") / "grafana").resolve(),
                    chart="grafana",
                    profile="minimal",
                    namespace="monitoring",
                )
            }
        ),
    )
    monkeypatch.setattr(
        service.lifecycle_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: _migration_plan(lint=True),
    )

    result = service.run(
        EphemeralTestRequest(
            chart="grafana",
            lint=True,
            ensure_cluster=False,
        )
    )

    assert calls == [
        "ready:monitoring:1m:app.kubernetes.io/instance=grafana",
        "test:grafana",
    ]
    assert result.installed == ()
    assert result.tested == ("grafana",)


def test_ephemeral_bootstrap_transitive_dependency_is_not_reinstalled_or_retested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, _kubectl = _migration_service(tmp_path, calls=calls)
    monkeypatch.setattr(
        LocalBootstrapExecutor,
        "preflight",
        lambda *_args, **_kwargs: frozenset(
            {
                ExternallySatisfiedLifecycle(
                    chart_path=(Path("charts") / "network").resolve(),
                    chart="network",
                    profile="minimal",
                    namespace="monitoring",
                )
            }
        ),
    )
    monkeypatch.setattr(
        service.lifecycle_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: _fanout_plan(
            "grafana", prerequisite=("network", "minimal"), lint=True
        ),
    )

    result = service.run(
        EphemeralTestRequest(chart="grafana", lint=True, ensure_cluster=False)
    )

    assert all("network" not in call for call in calls)
    assert "lint:network" not in calls
    assert calls.count("lint:grafana") == 1
    assert "install:grafana:grafana" in calls
    assert "test:grafana" in calls
    assert result.tested == ("grafana",)


def test_ephemeral_recomputes_bootstrap_satisfaction_for_every_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, _kubectl = _migration_service(tmp_path, calls=calls)
    identities = iter(
        (
            frozenset(
                {
                    ExternallySatisfiedLifecycle(
                        chart_path=(Path("charts") / "grafana").resolve(),
                        chart="grafana",
                        profile="minimal",
                        namespace="monitoring",
                    )
                }
            ),
            frozenset(),
        )
    )
    monkeypatch.setattr(
        LocalBootstrapExecutor,
        "preflight",
        lambda *_args, **_kwargs: next(identities),
    )
    monkeypatch.setattr(
        service.lifecycle_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: _migration_plan(),
    )

    service.run(EphemeralTestRequest(chart="grafana", ensure_cluster=False))
    first_run_calls = tuple(calls)
    calls.clear()
    service.run(EphemeralTestRequest(chart="grafana", ensure_cluster=False))

    assert first_run_calls == (
        "ready:monitoring:1m:app.kubernetes.io/instance=grafana",
        "test:grafana",
    )
    assert "install:grafana:grafana" in calls


def test_ephemeral_dependent_fanout_dedupes_shared_profile_and_preserves_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, _kubectl = _migration_service(tmp_path, calls=calls)
    dependents = (
        SimpleNamespace(chart="dependent-a", profile="minimal"),
        SimpleNamespace(chart="dependent-b", profile="minimal"),
    )
    monkeypatch.setattr(service.resolver, "dependent_tests", lambda _chart: dependents)
    plans = {
        ("main", "minimal"): _fanout_plan(
            "main", prerequisite=("shared", "minimal")
        ),
        ("dependent-a", "minimal"): _fanout_plan(
            "dependent-a", prerequisite=("shared", "minimal")
        ),
        ("dependent-b", "minimal"): _fanout_plan(
            "dependent-b", prerequisite=("shared", "minimal")
        ),
    }
    monkeypatch.setattr(
        service.lifecycle_compiler,
        "compile_cluster_test",
        lambda chart, profile, **_kwargs: plans[(chart, profile)],
    )

    result = service.run(
        EphemeralTestRequest(
            chart="main",
            ensure_cluster=False,
            include_dependent_tests=True,
        )
    )

    assert calls.count("install:shared:shared") == 1
    assert calls.count("test:shared") == 1
    assert result.tested == ("shared", "main", "dependent-a", "dependent-b")


def test_ephemeral_fanout_reconverges_same_release_for_distinct_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, _kubectl = _migration_service(tmp_path, calls=calls)
    monkeypatch.setattr(
        service.resolver,
        "dependent_tests",
        lambda _chart: (SimpleNamespace(chart="main", profile="full"),),
    )
    monkeypatch.setattr(
        service.lifecycle_compiler,
        "compile_cluster_test",
        lambda chart, profile, **_kwargs: _fanout_plan(chart, profile),
    )

    result = service.run(
        EphemeralTestRequest(
            chart="main",
            profile="minimal",
            ensure_cluster=False,
            include_dependent_tests=True,
        )
    )

    assert calls.count("install:main:main") == 2
    assert calls.count("test:main") == 2
    assert result.tested == ("main", "main")
