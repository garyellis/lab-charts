from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from chart_manager.api.lifecycle.v1alpha1 import ClusterTestProfile, ClusterTestSpec
from chart_manager.domain.lifecycle_policy import (
    load_chart_lifecycle,
    require_cluster_test,
    require_cluster_test_profile,
)
from chart_manager.plumbing.commands import CommandResult
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError, SpecError
from chart_manager.services.clusters.bootstrap import LocalBootstrapExecutor
from chart_manager.services.clusters.environment import (
    BoundClients,
    EnvironmentHandle,
    EnvironmentSpec,
)
from chart_manager.services.clusters.ephemeral import (
    EphemeralTeardownRequest,
    EphemeralTestClusterService,
    EphemeralTestRequest,
    _merge_lifecycle_plans,
)
from chart_manager.services.lifecycle.compiler import ClusterTestCompiler
from chart_manager.services.lifecycle.models import (
    ActionKind,
    ActionTarget,
    LifecycleAction,
    LifecyclePlan,
)
from chart_manager.services.lifecycle.plan_projection import ExternallySatisfiedLifecycle

from .conftest import MakeChart, cli


def _alloy_spec() -> ClusterTestSpec:
    lifecycle = load_chart_lifecycle(Path("charts/alloy/chart-lifecycle.yaml"))
    return require_cluster_test(lifecycle, chart_name="alloy")


def test_load_test_spec_accepts_chart_refs() -> None:
    spec = _alloy_spec()

    minimal = require_cluster_test_profile(spec, "minimal")

    assert minimal.requires[0].chart == "prometheus-operator"
    assert minimal.requires[0].profile == "minimal"
    assert minimal.helm_test is True


def test_unknown_profile_raises_spec_error() -> None:
    spec = _alloy_spec()

    with pytest.raises(SpecError):
        require_cluster_test_profile(spec, "missing")


def test_dependent_tests_is_the_only_authored_reverse_target_field() -> None:
    spec = ClusterTestSpec.model_validate(
        {
            "profiles": {"minimal": {"namespace": "default"}},
            "dependentTests": [{"chart": "grafana", "profile": "with-deps"}],
        }
    )

    assert [(ref.chart, ref.profile) for ref in spec.dependent_tests] == [
        ("grafana", "with-deps")
    ]

    with pytest.raises(ValidationError, match="reverseTests"):
        ClusterTestSpec.model_validate(
            {
                "profiles": {"minimal": {"namespace": "default"}},
                "reverseTests": [{"chart": "grafana"}],
            }
        )


def test_cli_exposes_dependent_tests_only_on_chart_test() -> None:
    root_help = cli("--help")
    chart_test_help = cli("chart", "test", "--help")

    assert root_help.exit_code == 0
    assert "deps" not in root_help.stdout
    assert chart_test_help.exit_code == 0
    assert "--dependent-tests" in chart_test_help.stdout
    assert "--reverse" not in chart_test_help.stdout


def test_cluster_test_profile_defaults_to_running_helm_tests() -> None:
    assert ClusterTestProfile(namespace="default").helm_test is True


def test_cluster_test_profile_accepts_disabled_helm_tests() -> None:
    assert ClusterTestProfile(namespace="default", helmTest=False).helm_test is False


def test_cluster_test_profile_rejects_removed_checks_configuration() -> None:
    with pytest.raises(ValidationError, match="checks"):
        ClusterTestProfile.model_validate(
            {
                "namespace": "default",
                "checks": [{"name": "pods-ready", "type": "helm-test"}],
            }
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
        missing_releases: set[str] | None = None,
    ) -> None:
        self.calls = calls
        self.fail_dependency = fail_dependency
        self.fail_lint = fail_lint
        self.missing_releases = missing_releases or set()

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

    def status(self, release: str, *, namespace: str) -> CommandResult:
        self.calls.append(f"status:{release}:{namespace}")
        missing = release in self.missing_releases
        return CommandResult(
            args=("helm", "status", release),
            returncode=1 if missing else 0,
            stdout="",
            stderr=f"Error: release: {release}: not found" if missing else "",
        )


def _migration_action(chart: str, suffix: str, kind: ActionKind) -> LifecycleAction:
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
    missing_releases: set[str] | None = None,
    environment_provider: object | None = None,
) -> tuple[EphemeralTestClusterService, _MigrationKubectl]:
    (tmp_path / "kind-config.yaml").write_text("kind: Cluster\n", encoding="utf-8")
    local_cluster = tmp_path / ".chart-manager/local-cluster.yaml"
    local_cluster.parent.mkdir()
    local_cluster.write_text(
        """
apiVersion: local.chartmanager.io/v1alpha1
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
            missing_releases=missing_releases,
        ),  # type: ignore[arg-type]
        kind=_MigrationKind(),  # type: ignore[arg-type]
        kubectl=kubectl,  # type: ignore[arg-type]
        environment_provider=environment_provider,  # type: ignore[arg-type]
    )
    return service, kubectl


def test_ephemeral_default_executes_projected_action_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, _kubectl = _migration_service(tmp_path, calls=calls)
    monkeypatch.setattr(
        service.cluster_test_compiler,
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

    def bind(handle: Any) -> BoundClients:
        handles.append(handle)
        # One factory shape serves both cluster services; this one owns no
        # port-forward, so it never reads `expose`.
        return BoundClients(
            helm=helm,
            kubectl=kubectl,
            expose=None,  # type: ignore[arg-type]
        )

    service._client_factory = bind  # type: ignore[assignment]
    monkeypatch.setattr(
        service.cluster_test_compiler,
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
        service.cluster_test_compiler,
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
        service.cluster_test_compiler,
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


def test_ephemeral_plan_compiles_the_run_without_touching_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`plan` is `run`'s compile step and nothing else.

    `calls == []` is the whole assertion: the helm, kind and kubectl fakes
    record every invocation, so an empty list is proof that a `--dry-run`
    created no cluster, updated no dependency and installed nothing --
    including under `ensure_cluster=True`, which is the default and is what
    a caller who only added `--dry-run` will have set.
    """
    calls: list[str] = []
    service, kubectl = _migration_service(tmp_path, calls=calls)
    monkeypatch.setattr(
        service.cluster_test_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: _migration_plan(),
    )

    plan = service.plan(EphemeralTestRequest(chart="grafana"))

    assert plan.chart == "grafana"
    assert [action.kind for action in plan.actions] == [
        action.kind for action in _migration_plan().actions
    ]
    assert calls == []
    assert kubectl.diagnostic_namespaces == []


def test_ephemeral_plan_keeps_the_lint_action_it_would_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--lint` still shapes the plan, though the dry run lints nothing.

    `plan` calls the bootstrap preflight with `lint=False` so no `helm lint`
    subprocess runs; the request's own `lint` reaches the compiler, so the
    printed plan still shows the HELM_LINT action a real run would execute.
    Those two are easy to conflate, which is why they are asserted together.
    """
    calls: list[str] = []
    service, _kubectl = _migration_service(tmp_path, calls=calls)
    monkeypatch.setattr(
        service.cluster_test_compiler,
        "compile_cluster_test",
        lambda *_args, **kwargs: _migration_plan(lint=kwargs["lint"]),
    )

    plan = service.plan(EphemeralTestRequest(chart="grafana", lint=True))

    assert ActionKind.HELM_LINT in [action.kind for action in plan.actions]
    assert calls == []


def test_ephemeral_lint_failure_keeps_diagnostics_and_terminal_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, kubectl = _migration_service(tmp_path, calls=calls, fail_lint=True)
    monkeypatch.setattr(
        service.cluster_test_compiler,
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
        service.cluster_test_compiler,
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
        service.cluster_test_compiler,
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


def test_skip_requires_runs_only_target_and_never_tests_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, _kubectl = _migration_service(tmp_path, calls=calls)
    monkeypatch.setattr(
        service.cluster_test_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: _fanout_plan(
            "app", prerequisite=("shared", "minimal")
        ),
    )

    result = service.run(
        EphemeralTestRequest(chart="app", skip_requires=True, ensure_cluster=False)
    )

    assert calls == [
        "status:shared:monitoring",
        "namespace:monitoring",
        "dependency:app",
        "install:app:app",
        "ready:monitoring:1m:app.kubernetes.io/instance=app",
        "test:app",
    ]
    assert "test:shared" not in calls
    assert result.installed == ("app",)
    assert result.tested == ("app",)


def test_skip_requires_missing_release_fails_before_any_install_or_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, _kubectl = _migration_service(
        tmp_path,
        calls=calls,
        missing_releases={"shared"},
    )
    monkeypatch.setattr(
        service.cluster_test_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: _fanout_plan(
            "app", prerequisite=("shared", "minimal")
        ),
    )

    with pytest.raises(
        ChartManagerError,
        match=r"app requires shared:minimal, not installed in monitoring; "
        r"run once without --skip-requires",
    ):
        service.run(
            EphemeralTestRequest(chart="app", skip_requires=True, ensure_cluster=False)
        )

    assert calls == ["status:shared:monitoring"]


def test_skip_requires_dry_run_lists_assumptions_without_status_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, _kubectl = _migration_service(tmp_path, calls=calls)
    monkeypatch.setattr(
        service.cluster_test_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: _fanout_plan(
            "app", prerequisite=("shared", "minimal")
        ),
    )

    plan = service.plan(EphemeralTestRequest(chart="app", skip_requires=True))

    assert {action.target.chart for action in plan.actions} == {"app"}
    assert all(action.target.chart != "shared" for action in plan.actions)
    assert plan.warnings[-1].endswith("shared:minimal")
    assert calls == []


def test_skip_requires_does_not_preflight_bootstrap_owned_requirement(
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
                    chart_path=(Path("charts") / "shared").resolve(),
                    chart="shared",
                    profile="minimal",
                    namespace="monitoring",
                )
            }
        ),
    )
    monkeypatch.setattr(
        service.cluster_test_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: _fanout_plan(
            "app", prerequisite=("shared", "minimal")
        ),
    )

    service.run(EphemeralTestRequest(chart="app", skip_requires=True, ensure_cluster=False))

    assert not [call for call in calls if call.startswith("status:")]
    assert "test:shared" not in calls


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
        service.cluster_test_compiler,
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
        service.cluster_test_compiler,
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


def test_skip_requires_applies_to_dependent_test_fanout_requirements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    service, _kubectl = _migration_service(tmp_path, calls=calls)
    dependents = (SimpleNamespace(chart="dependent", profile="minimal"),)
    monkeypatch.setattr(service.resolver, "dependent_tests", lambda _chart: dependents)
    plans = {
        ("main", "minimal"): _fanout_plan(
            "main", prerequisite=("shared", "minimal")
        ),
        ("dependent", "minimal"): _fanout_plan(
            "dependent", prerequisite=("shared", "minimal")
        ),
    }
    monkeypatch.setattr(
        service.cluster_test_compiler,
        "compile_cluster_test",
        lambda chart, profile, **_kwargs: plans[(chart, profile)],
    )

    result = service.run(
        EphemeralTestRequest(
            chart="main",
            ensure_cluster=False,
            include_dependent_tests=True,
            skip_requires=True,
        )
    )

    assert calls.count("status:shared:monitoring") == 1
    assert "install:shared:shared" not in calls
    assert "test:shared" not in calls
    assert result.tested == ("main", "dependent")


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
        service.cluster_test_compiler,
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


def test_merged_fanout_plans_keep_one_reverse_install_order_cleanup_tail(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    script = chart_root / "scripts" / "hook"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")

    def hooked(chart: str) -> dict[str, object]:
        return {
            "hooks": {
                "preInstall": ["scripts/hook", chart],
                "cleanup": ["scripts/hook", chart],
            }
        }

    make_chart("dep", profiles={"minimal": hooked("dep")})
    for dependent in ("a", "b"):
        make_chart(
            dependent,
            profiles={
                "minimal": {
                    "requires": [{"chart": "dep", "profile": "minimal"}],
                    **hooked(dependent),
                }
            },
        )
    compiler = ClusterTestCompiler(chart_root)

    merged = _merge_lifecycle_plans(
        [
            compiler.compile_cluster_test("a", "minimal"),
            compiler.compile_cluster_test("b", "minimal"),
        ]
    )

    hook_ids = [
        action.action_id.removeprefix("cluster-test.")
        for action in merged.actions
        if action.kind in (ActionKind.HOOK_PRE_INSTALL, ActionKind.HOOK_CLEANUP)
    ]
    assert hook_ids == [
        "dep.minimal.hook-pre-install",
        "a.minimal.hook-pre-install",
        "b.minimal.hook-pre-install",
        "b.minimal.hook-cleanup",
        "a.minimal.hook-cleanup",
        "dep.minimal.hook-cleanup",
    ]
    assert [action.kind for action in merged.actions[-3:]] == [ActionKind.HOOK_CLEANUP] * 3


def test_ephemeral_runs_install_hooks_against_the_bound_cluster_and_never_cleanup(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    record = chart_root / "hook-record"
    script = chart_root / "scripts" / "hook"
    script.parent.mkdir()
    script.write_text(
        "#!/bin/sh\n"
        'echo "$CHART_MANAGER_HOOK_PHASE $CHART_MANAGER_KUBE_CONTEXT '
        f'$CHART_MANAGER_CLUSTER_NAME" >> {record}\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    make_chart(
        "app",
        profiles={
            "minimal": {
                "namespace": "apps",
                "helmTest": False,
                "hooks": {
                    "preInstall": ["./scripts/hook"],
                    "postInstall": ["./scripts/hook"],
                    "cleanup": ["./scripts/hook"],
                },
            }
        },
    )
    calls: list[str] = []
    service, _kubectl = _migration_service(chart_root, calls=calls)

    result = service.run(
        EphemeralTestRequest(chart="app", cluster_name="lab", ensure_cluster=False)
    )

    assert record.read_text(encoding="utf-8").splitlines() == [
        "pre-install kind-lab lab",
        "post-install kind-lab lab",
    ]
    assert result.installed == ("app",)


def test_ephemeral_failed_pre_install_hook_fails_the_run_before_install(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    script = chart_root / "scripts" / "hook"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\necho 'no credential' >&2\nexit 3\n", encoding="utf-8")
    script.chmod(0o755)
    make_chart(
        "app",
        profiles={"minimal": {"hooks": {"preInstall": ["./scripts/hook"]}}},
    )
    calls: list[str] = []
    service, _kubectl = _migration_service(chart_root, calls=calls)

    with pytest.raises(
        ChartManagerError,
        match=r"cluster action failed for app \(hook-pre-install\): "
        r"pre-install hook exited 3: ./scripts/hook\nno credential",
    ):
        service.run(EphemeralTestRequest(chart="app", ensure_cluster=False))

    assert not any(call.startswith("install:") for call in calls)


# ----- chart teardown ---------------------------------------------------------


class _TeardownProvider:
    def __init__(self, record: Path, *, exists: bool = True, fail_destroy: bool = False) -> None:
        self.record = record
        self.exists = exists
        self.fail_destroy = fail_destroy

    def _write(self, line: str) -> None:
        with self.record.open("a", encoding="utf-8") as stream:
            stream.write(f"{line}\n")

    def _handle(self, spec: EnvironmentSpec) -> EnvironmentHandle:
        return EnvironmentHandle(
            identity=spec.cluster_name,
            context=f"kind-{spec.cluster_name}",
            provider_type="kind",
        )

    def ensure(self, spec: EnvironmentSpec) -> EnvironmentHandle:
        self._write(f"ensure {spec.cluster_name}")
        return self._handle(spec)

    def inspect(self, spec: EnvironmentSpec) -> EnvironmentHandle | None:
        return self._handle(spec) if self.exists else None

    def handle(self, spec: EnvironmentSpec) -> EnvironmentHandle:
        return self._handle(spec)

    def destroy(self, handle: EnvironmentHandle) -> bool:
        self._write(f"destroy {handle.identity}")
        if self.fail_destroy:
            raise ExternalCommandError("kind delete cluster failed")
        return True


def _teardown_charts(chart_root: Path, make_chart: MakeChart, *, fail: str = "") -> Path:
    """`app` requires `base`; each has a preInstall and a recording cleanup."""
    record = chart_root / "hook-record"
    script = chart_root / "scripts" / "hook"
    script.parent.mkdir()
    script.write_text(
        "#!/bin/sh\n"
        'echo "$CHART_MANAGER_HOOK_PHASE $CHART_MANAGER_CHART '
        f'[$CHART_MANAGER_KUBE_CONTEXT] $CHART_MANAGER_CLUSTER_NAME" >> {record}\n'
        '[ "$1" = fail ] && { echo "still attached" >&2; exit 3; }\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    def hooks(chart: str) -> dict[str, list[str]]:
        cleanup = ["./scripts/hook", *(["fail"] if chart == fail else [])]
        return {"preInstall": ["./scripts/hook"], "cleanup": cleanup}

    make_chart("base", profiles={"minimal": {"namespace": "base", "hooks": hooks("base")}})
    make_chart(
        "app",
        profiles={
            "minimal": {
                "namespace": "apps",
                "requires": [{"chart": "base", "profile": "minimal"}],
                "hooks": hooks("app"),
            }
        },
    )
    return record


def _teardown_service(
    chart_root: Path, record: Path, **provider: bool
) -> tuple[EphemeralTestClusterService, list[str]]:
    calls: list[str] = []
    service, _kubectl = _migration_service(
        chart_root,
        calls=calls,
        environment_provider=_TeardownProvider(record, **provider),
    )
    return service, calls


def test_teardown_runs_cleanups_in_reverse_install_order_then_deletes_the_cluster(
    chart_root: Path, make_chart: MakeChart
) -> None:
    record = _teardown_charts(chart_root, make_chart)
    service, calls = _teardown_service(chart_root, record)

    result = service.teardown(EphemeralTeardownRequest(chart="app", cluster_name="lab"))

    assert record.read_text(encoding="utf-8").splitlines() == [
        "cleanup app [kind-lab] lab",
        "cleanup base [kind-lab] lab",
        "destroy lab",
    ]
    assert calls == []
    assert result.ok
    assert result.cluster_deleted
    assert [(o.action_id, o.verdict) for o in result.cleanups] == [
        ("cluster-test.app.minimal.hook-cleanup", "PASS"),
        ("cluster-test.base.minimal.hook-cleanup", "PASS"),
    ]


def test_skip_requires_does_not_remove_required_cleanup_from_teardown(
    chart_root: Path, make_chart: MakeChart
) -> None:
    record = _teardown_charts(chart_root, make_chart)
    service, _calls = _teardown_service(chart_root, record)

    test_plan = service.plan(EphemeralTestRequest(chart="app", skip_requires=True))
    teardown_plan = service.teardown_plan(EphemeralTeardownRequest(chart="app"))

    assert not [
        action
        for action in test_plan.actions
        if action.target.chart == "base"
    ]
    assert [action.target.chart for action in teardown_plan.actions] == ["app", "base"]
    assert [action.kind for action in teardown_plan.actions] == [ActionKind.HOOK_CLEANUP] * 2
    assert not record.exists()


def test_teardown_continues_past_a_failed_cleanup_and_still_deletes_the_cluster(
    chart_root: Path, make_chart: MakeChart
) -> None:
    record = _teardown_charts(chart_root, make_chart, fail="app")
    service, _calls = _teardown_service(chart_root, record)

    result = service.teardown(EphemeralTeardownRequest(chart="app", cluster_name="lab"))

    assert record.read_text(encoding="utf-8").splitlines() == [
        "cleanup app [kind-lab] lab",
        "cleanup base [kind-lab] lab",
        "destroy lab",
    ]
    assert not result.ok
    assert result.cluster_deleted
    failed, passed = result.cleanups
    assert (failed.verdict, passed.verdict) == ("FAIL", "PASS")
    assert failed.detail == "cleanup hook exited 3: ./scripts/hook fail\nstill attached"


def test_teardown_keep_cluster_runs_cleanups_without_deleting(
    chart_root: Path, make_chart: MakeChart
) -> None:
    record = _teardown_charts(chart_root, make_chart)
    service, _calls = _teardown_service(chart_root, record)

    result = service.teardown(
        EphemeralTeardownRequest(chart="app", cluster_name="lab", keep_cluster=True)
    )

    assert record.read_text(encoding="utf-8").splitlines() == [
        "cleanup app [kind-lab] lab",
        "cleanup base [kind-lab] lab",
    ]
    assert result.ok
    assert not result.cluster_deleted


def test_teardown_of_a_missing_cluster_still_runs_cleanups_and_creates_nothing(
    chart_root: Path, make_chart: MakeChart
) -> None:
    record = _teardown_charts(chart_root, make_chart)
    events: list[Any] = []
    service, _calls = _teardown_service(chart_root, record, exists=False)
    service._progress = events.append

    result = service.teardown(EphemeralTeardownRequest(chart="app", cluster_name="lab"))

    assert record.read_text(encoding="utf-8").splitlines() == [
        "cleanup app [] lab",
        "cleanup base [] lab",
    ]
    assert result.ok
    assert not result.cluster_deleted
    assert any("lab does not exist" in event.message for event in events)


def test_teardown_delete_failure_is_reported_on_the_result(
    chart_root: Path, make_chart: MakeChart
) -> None:
    record = _teardown_charts(chart_root, make_chart)
    service, _calls = _teardown_service(chart_root, record, fail_destroy=True)

    result = service.teardown(EphemeralTeardownRequest(chart="app", cluster_name="lab"))

    assert not result.ok
    assert not result.cluster_deleted
    assert result.delete_error == "kind delete cluster failed"


def test_teardown_without_cleanups_only_deletes_the_cluster(
    chart_root: Path, make_chart: MakeChart
) -> None:
    record = chart_root / "hook-record"
    make_chart("app", profiles={"minimal": {"namespace": "apps"}})
    service, _calls = _teardown_service(chart_root, record)

    result = service.teardown(EphemeralTeardownRequest(chart="app", cluster_name="lab"))

    assert record.read_text(encoding="utf-8").splitlines() == ["destroy lab"]
    assert result.ok
    assert result.cleanups == ()


def test_teardown_plan_is_the_test_plans_cleanup_tail_and_runs_nothing(
    chart_root: Path, make_chart: MakeChart
) -> None:
    record = _teardown_charts(chart_root, make_chart)
    service, _calls = _teardown_service(chart_root, record)

    plan = service.teardown_plan(EphemeralTeardownRequest(chart="app"))

    full = service.plan(EphemeralTestRequest(chart="app"))
    assert plan.actions == full.actions[-2:]
    assert [action.kind for action in plan.actions] == [ActionKind.HOOK_CLEANUP] * 2
    assert not record.exists()
