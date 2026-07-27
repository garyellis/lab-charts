from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from chart_manager.cli.main import app
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.chart_config import (
    load_chart_lifecycle,
    require_cluster_test,
)
from chart_manager.services.clusters import ephemeral as ephemeral_module
from chart_manager.services.clusters.ephemeral import (
    EphemeralTestClusterService,
    EphemeralTestRequest,
)
from chart_manager.services.domain.cluster_tests import (
    ClusterCheckSpec,
    ClusterTestProfile,
    ClusterTestSpec,
    SpecError,
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


def _alloy_spec() -> ClusterTestSpec:
    lifecycle = load_chart_lifecycle(Path("charts/alloy/chart-lifecycle.yaml"))
    return require_cluster_test(lifecycle, chart_name="alloy")


def test_load_test_spec_accepts_chart_refs() -> None:
    spec = _alloy_spec()

    minimal = spec.profile("minimal")

    assert minimal.requires[0].chart == "prometheus-operator"
    assert minimal.requires[0].profile == "minimal"
    assert minimal.helm_test is True
    assert minimal.checks[0].name == "alloy-pods-ready"


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


def test_cli_exposes_only_dependent_test_vocabulary() -> None:
    runner = CliRunner()

    deps_help = runner.invoke(app, ["deps", "--help"])
    sandbox_help = runner.invoke(app, ["sandbox", "test", "--help"])

    assert deps_help.exit_code == 0
    assert "dependent-tests" in deps_help.stdout
    assert "reverse" not in deps_help.stdout
    assert sandbox_help.exit_code == 0
    assert "--dependent-tests" in sandbox_help.stdout
    assert "--reverse" not in sandbox_help.stdout


# ----- ClusterTestProfile.effective_checks ---------------------------------
#
# The implicit helm-test check used to be synthesized in `cli/main.py`'s
# `deps checks` handler. It is a domain rule with one correct answer, so it
# lives on the model and every surface sees the same list.


def test_effective_checks_appends_implicit_helm_test() -> None:
    profile = ClusterTestProfile(
        helmTest=True,
        checks=[ClusterCheckSpec(name="pods-ready", type="pod")],
    )

    checks = profile.effective_checks()

    assert [c.name for c in checks] == ["pods-ready", "helm-test"]
    assert checks[-1].type == "helm-test"
    assert checks[-1].description == "Run Helm test hooks for the release."


def test_effective_checks_omits_implicit_check_when_helm_test_disabled() -> None:
    profile = ClusterTestProfile(
        helmTest=False,
        checks=[ClusterCheckSpec(name="pods-ready", type="pod")],
    )

    assert [c.name for c in profile.effective_checks()] == ["pods-ready"]


def test_effective_checks_does_not_duplicate_an_explicit_helm_test_check() -> None:
    # An explicitly declared helm-test check wins: the profile author gets
    # to name and describe it.
    explicit = ClusterCheckSpec(name="my-smoke", type="helm-test", description="custom")
    profile = ClusterTestProfile(helmTest=True, checks=[explicit])

    assert profile.effective_checks() == [explicit]


def test_effective_checks_returns_a_fresh_list() -> None:
    # Callers mutate the returned list (the CLI used to); it must not
    # alias the model's own `checks`.
    profile = ClusterTestProfile(helmTest=True, checks=[])

    profile.effective_checks().append(ClusterCheckSpec(name="x"))

    assert profile.checks == []


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
    def __init__(self, calls: list[str], *, fail_dependency: bool = False) -> None:
        self.calls = calls
        self.fail_dependency = fail_dependency

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

    def lint(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls.append("lint")


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


def _migration_plan(*, include_cilium: bool = True) -> LifecyclePlan:
    namespace = _migration_action("grafana", "namespace", ActionKind.NAMESPACE_ENSURE)
    dependency = _migration_action(
        "grafana", "dependency", ActionKind.HELM_DEPENDENCY_UPDATE
    )
    install = _migration_action("grafana", "install", ActionKind.HELM_UPGRADE_INSTALL)
    ready = _migration_action("grafana", "ready", ActionKind.WORKLOAD_READY)
    test = _migration_action("grafana", "test", ActionKind.HELM_TEST)
    actions = [namespace, dependency, install, ready, test]
    edges = [
        LifecycleEdge(namespace.action_id, install.action_id, EdgeKind.SEQUENCE),
        LifecycleEdge(dependency.action_id, install.action_id, EdgeKind.SEQUENCE),
        LifecycleEdge(install.action_id, ready.action_id, EdgeKind.SEQUENCE),
        LifecycleEdge(ready.action_id, test.action_id, EdgeKind.SEQUENCE),
    ]
    if include_cilium:
        cilium = _migration_action(
            "cilium", "bootstrap-owned", ActionKind.HELM_UPGRADE_INSTALL
        )
        actions.insert(0, cilium)
        edges.append(
            LifecycleEdge(
                cilium.action_id,
                install.action_id,
                EdgeKind.RUNTIME_REQUIREMENT,
            )
        )
    return LifecyclePlan(
        workflow=Workflow.CLUSTER_TEST,
        chart="grafana",
        profile="minimal",
        actions=tuple(actions),
        edges=tuple(edges),
    )


def _migration_service(
    tmp_path: Path,
    *,
    calls: list[str],
    fail_dependency: bool = False,
) -> tuple[EphemeralTestClusterService, _MigrationKubectl]:
    kubectl = _MigrationKubectl(calls)
    service = EphemeralTestClusterService(
        tmp_path,
        helm=_MigrationHelm(calls, fail_dependency=fail_dependency),  # type: ignore[arg-type]
        kind=_MigrationKind(),  # type: ignore[arg-type]
        kubectl=kubectl,  # type: ignore[arg-type]
    )
    return service, kubectl


def test_ephemeral_default_executes_projected_action_order_and_excludes_cilium(
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
    monkeypatch.setattr(
        ephemeral_module.cluster_bootstrap,
        "bootstrap",
        lambda *_args, **_kwargs: "deployed",
    )

    result = service.run(EphemeralTestRequest(chart="grafana", ensure_cluster=False))

    assert calls == [
        "namespace:monitoring",
        "dependency:grafana",
        "install:grafana:grafana",
        "ready:monitoring:1m:app.kubernetes.io/instance=grafana",
        "test:grafana",
    ]
    assert result.installed == ("cilium", "grafana")
    assert result.tested == ("grafana",)
    assert result.namespaces == ("kube-system", "monitoring")
    history = service.evidence_repository.history()
    assert len(history.records) == 5
    assert {record.target.chart for record in history.records} == {"grafana"}
    assert {record.cluster.context for record in history.records if record.cluster} == {
        "kind-configured"
    }


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
        lambda *_args, **_kwargs: _migration_plan(include_cilium=False),
    )
    monkeypatch.setattr(
        ephemeral_module.cluster_bootstrap,
        "bootstrap",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ChartManagerError, match="dependency update failed"):
        service.run(EphemeralTestRequest(chart="grafana", ensure_cluster=False))

    history = service.evidence_repository.history()
    assert {record.verdict for record in history.records} == {"PASS", "FAIL", "SKIP"}
    assert len(history.records) == 5
    assert kubectl.diagnostic_namespaces == ["monitoring"]
    assert "install:grafana:grafana" not in calls


@pytest.mark.parametrize(
    ("chart", "lint"),
    (("grafana", True), ("cilium", False)),
)
def test_ephemeral_lint_and_cilium_targets_retain_legacy_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chart: str,
    lint: bool,
) -> None:
    calls: list[str] = []
    service, _kubectl = _migration_service(tmp_path, calls=calls)
    legacy_calls: list[bool] = []
    monkeypatch.setattr(service.resolver, "install_plan", lambda *_args: [])
    monkeypatch.setattr(
        service,
        "_install_plan",
        lambda *_args, lint, **_kwargs: legacy_calls.append(lint),
    )
    monkeypatch.setattr(
        service.lifecycle_compiler,
        "compile_cluster_test",
        lambda *_args, **_kwargs: pytest.fail("lifecycle path must not run"),
    )
    monkeypatch.setattr(
        ephemeral_module.cluster_bootstrap,
        "bootstrap",
        lambda *_args, **_kwargs: None,
    )

    service.run(
        EphemeralTestRequest(
            chart=chart,
            lint=lint,
            ensure_cluster=False,
        )
    )

    assert legacy_calls == [lint]
