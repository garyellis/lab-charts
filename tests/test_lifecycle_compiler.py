"""Lifecycle compiler contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from chart_manager.plumbing.errors import (
    ChartManagerError,
    DependencyCycleError,
    SpecError,
)
from chart_manager.services.lifecycle import (
    ActionKind,
    LifecycleCompiler,
    Workflow,
)

from .conftest import MakeChart


def _add_validation(chart: Path) -> None:
    lifecycle = yaml.safe_load((chart / "chart-lifecycle.yaml").read_text())
    lifecycle["spec"]["validation"] = {
        "releaseName": chart.name,
        "namespaceTemplate": "lab-${env}",
        "environments": {
            "dev": {"values": ["values.yaml", "values-dev.yaml"]},
        },
        "schemaLocations": ["default"],
    }
    (chart / "values-dev.yaml").write_text("replicas: 1\n")
    (chart / "chart-lifecycle.yaml").write_text(yaml.safe_dump(lifecycle))


def _requires(*refs: str) -> dict[str, object]:
    parsed = []
    for ref in refs:
        chart, _, profile = ref.partition(":")
        parsed.append({"chart": chart, "profile": profile or "minimal"})
    return {"requires": parsed}


def test_validation_compiles_the_authored_environment_to_an_ordered_plan(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("app")
    _add_validation(chart)

    plan = LifecycleCompiler(chart_root).compile_validation("app", "dev")

    assert plan.workflow is Workflow.VALIDATION
    assert plan.environment == "dev"
    assert [action.kind for action in plan.actions] == [
        ActionKind.HELM_DEPENDENCY_UPDATE,
        ActionKind.RENDER,
        ActionKind.SCHEMA_VALIDATE,
        ActionKind.POLICY_VALIDATE,
    ]
    assert plan.actions[1].target.to_dict() == {
        "workflow": "validation",
        "chart": "app",
        "environment": "dev",
        "release": "app",
        "namespace": "lab-dev",
    }
    assert [path.name for path in plan.actions[1].values] == [
        "values.yaml",
        "values-dev.yaml",
    ]
    assert all(action.input_digest.startswith("sha256:") for action in plan.actions)


@pytest.mark.parametrize(
    ("validators", "expected_actions"),
    [
        (
            {"kubeconform": False, "policy": True},
            [
                ActionKind.HELM_DEPENDENCY_UPDATE,
                ActionKind.RENDER,
                ActionKind.POLICY_VALIDATE,
            ],
        ),
        (
            {"kubeconform": True, "policy": False},
            [
                ActionKind.HELM_DEPENDENCY_UPDATE,
                ActionKind.RENDER,
                ActionKind.SCHEMA_VALIDATE,
            ],
        ),
        (
            {"kubeconform": False, "policy": False},
            [ActionKind.HELM_DEPENDENCY_UPDATE, ActionKind.RENDER],
        ),
    ],
)
def test_validation_plan_omits_disabled_validators(
    chart_root: Path,
    make_chart: MakeChart,
    validators: dict[str, bool],
    expected_actions: list[ActionKind],
) -> None:
    chart = make_chart("app")
    _add_validation(chart)
    lifecycle = yaml.safe_load((chart / "chart-lifecycle.yaml").read_text())
    lifecycle["spec"]["validation"]["validators"] = validators
    (chart / "chart-lifecycle.yaml").write_text(yaml.safe_dump(lifecycle))

    plan = LifecycleCompiler(chart_root).compile_validation("app", "dev")

    assert [action.kind for action in plan.actions] == expected_actions


def test_cluster_test_compiles_dependency_first_actions_and_effective_inputs(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart(
        "base",
        profiles={
            "minimal": {
                "namespace": "operators",
                "timeout": "7m",
                "values": ["values.yaml"],
            }
        },
    )
    make_chart(
        "app",
        profiles={
            "full": {
                **_requires("base"),
                "timeout": "20m",
                "values": ["values.yaml", "values-full.yaml"],
            }
        },
    )

    plan = LifecycleCompiler(chart_root).compile_cluster_test(
        "app",
        "full",
        default_namespace="workloads",
    )

    assert plan.workflow is Workflow.CLUSTER_TEST
    assert [action.target.chart for action in plan.actions] == [
        *(["base"] * 5),
        *(["app"] * 5),
    ]
    app_install = next(
        action
        for action in plan.actions
        if action.target.chart == "app"
        and action.kind is ActionKind.HELM_UPGRADE_INSTALL
    )
    assert app_install.target.namespace == "workloads"
    assert app_install.timeout == "20m"
    assert [path.name for path in app_install.values] == [
        "values.yaml",
        "values-full.yaml",
    ]
    assert app_install.metadata == ()
    assert all(action.metadata == () for action in plan.actions)


def test_cluster_test_namespace_override_wins_over_authored_profile(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("app", profiles={"minimal": {"namespace": "authored"}})

    plan = LifecycleCompiler(chart_root).compile_cluster_test(
        "app",
        "minimal",
        namespace_override="requested",
    )

    assert {
        action.target.namespace
        for action in plan.actions
        if action.target.namespace is not None
    } == {"requested"}


def test_cluster_test_namespace_override_does_not_relocate_authored_dependency(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("base", profiles={"minimal": {"namespace": "foundation"}})
    make_chart(
        "app",
        profiles={
            "minimal": {
                "namespace": "authored-app",
                "requires": [{"chart": "base", "profile": "minimal"}],
            }
        },
    )

    plan = LifecycleCompiler(chart_root).compile_cluster_test(
        "app",
        "minimal",
        namespace_override="requested-app",
    )

    namespaces = {
        action.target.chart: action.target.namespace
        for action in plan.actions
        if action.kind is ActionKind.HELM_UPGRADE_INSTALL
    }
    assert namespaces == {"base": "foundation", "app": "requested-app"}


def test_cluster_test_keeps_readiness_when_helm_test_is_disabled(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("app", profiles={"minimal": {"helmTest": False}})

    plan = LifecycleCompiler(chart_root).compile_cluster_test("app", "minimal")

    assert [action.kind for action in plan.actions] == [
        ActionKind.NAMESPACE_ENSURE,
        ActionKind.HELM_DEPENDENCY_UPDATE,
        ActionKind.HELM_UPGRADE_INSTALL,
        ActionKind.WORKLOAD_READY,
    ]


def test_cluster_test_lint_is_typed_and_ordered_between_dependency_and_install(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("app")

    plan = LifecycleCompiler(chart_root).compile_cluster_test(
        "app", "minimal", lint=True
    )

    assert [action.kind for action in plan.actions] == [
        ActionKind.NAMESPACE_ENSURE,
        ActionKind.HELM_DEPENDENCY_UPDATE,
        ActionKind.HELM_LINT,
        ActionKind.HELM_UPGRADE_INSTALL,
        ActionKind.WORKLOAD_READY,
        ActionKind.HELM_TEST,
    ]
    lint = next(action for action in plan.actions if action.kind is ActionKind.HELM_LINT)
    assert {path.name for path in lint.values} == {"values.yaml"}


def test_plan_projection_is_deterministic_and_json_serializable(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("app")
    compiler = LifecycleCompiler(chart_root)

    first = compiler.compile_cluster_test("app", "minimal").to_dict()
    second = compiler.compile_cluster_test("app", "minimal").to_dict()

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["apiVersion"] == "lifecycle.cmg.io/v1alpha1"
    assert first["kind"] == "LifecyclePlan"
    assert first["actions"][0]["actionId"].startswith("cluster-test.app.minimal.")
    assert first["actions"][0]["target"]["workflow"] == "cluster-test"
    assert "edges" not in first


def test_validation_rejects_unknown_environment_as_a_domain_error(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("app")
    _add_validation(chart)

    with pytest.raises(SpecError, match="unknown environment 'missing'"):
        LifecycleCompiler(chart_root).compile_validation("app", "missing")


def test_generated_dependency_contents_do_not_change_compiled_input_digest(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("app")
    compiler = LifecycleCompiler(chart_root)
    before = compiler.compile_cluster_test("app", "minimal")

    generated = chart / "charts"
    generated.mkdir()
    (generated / "dependency-1.2.3.tgz").write_bytes(b"downloaded later")
    after = compiler.compile_cluster_test("app", "minimal")

    assert [action.input_digest for action in before.actions] == [
        action.input_digest for action in after.actions
    ]

    templates = chart / "templates"
    templates.mkdir()
    (templates / "deployment.yaml").write_text("kind: Deployment\n")
    source_changed = compiler.compile_cluster_test("app", "minimal")

    assert [action.input_digest for action in after.actions] != [
        action.input_digest for action in source_changed.actions
    ]

    (chart / "Chart.lock").write_text("dependencies: []\n")
    lock_changed = compiler.compile_cluster_test("app", "minimal")
    assert [action.input_digest for action in source_changed.actions] != [
        action.input_digest for action in lock_changed.actions
    ]


def test_repository_policy_source_is_part_of_policy_action_digest(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("app")
    _add_validation(chart)
    policies = chart_root / "policies"
    policies.mkdir()
    policy = policies / "require-labels.yaml"
    policy.write_text("apiVersion: kyverno.io/v1\n")
    compiler = LifecycleCompiler(chart_root)
    before = compiler.compile_validation("app", "dev")

    policy.write_text("apiVersion: kyverno.io/v2\n")
    after = compiler.compile_validation("app", "dev")

    before_by_kind = {action.kind: action.input_digest for action in before.actions}
    after_by_kind = {action.kind: action.input_digest for action in after.actions}
    assert (
        before_by_kind[ActionKind.POLICY_VALIDATE]
        != after_by_kind[ActionKind.POLICY_VALIDATE]
    )
    assert (
        before_by_kind[ActionKind.RENDER]
        == after_by_kind[ActionKind.RENDER]
    )


def test_validation_digest_includes_toolchain_and_execution_engine_sources(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("app")
    _add_validation(chart)
    compiler = LifecycleCompiler(chart_root)
    before = compiler.compile_validation("app", "dev")

    (chart_root / ".mise.toml").write_text('[tools]\nhelm = "3.20.0"\n')
    pins_changed = compiler.compile_validation("app", "dev")
    assert [action.input_digest for action in before.actions] != [
        action.input_digest for action in pins_changed.actions
    ]

    engine = (
        chart_root
        / "src"
        / "chart_manager"
        / "services"
        / "manifest_validation"
    )
    engine.mkdir(parents=True)
    (engine / "engine.py").write_text("PHASES = ('render',)\n")
    source_changed = compiler.compile_validation("app", "dev")
    assert [action.input_digest for action in pins_changed.actions] != [
        action.input_digest for action in source_changed.actions
    ]


def test_digest_rejects_value_symlink_that_escapes_repository_root(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("app")
    outside = chart_root.parent / "outside-values.yaml"
    outside.write_text("{}\n")
    values = chart / "values.yaml"
    values.unlink()
    values.symlink_to(outside)

    with pytest.raises(SpecError, match="digest input escapes repository root"):
        LifecycleCompiler(chart_root).compile_cluster_test("app", "minimal")


def test_compile_rejects_a_requires_cycle(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    """A `requires` cycle fails at compile time, not silently mid-install.

    This and the two tests below are what remains of the deleted `lifecycle
    doctor` command. Doctor checked the whole repository up front; the
    compiler checks the chart:profile actually being compiled. Since every
    execution path (`charts test`, `local up`) compiles before
    it mutates anything, a broken reference on a chart anyone exercises still
    fails loudly -- see `DependencyResolver.install_plan`.
    """
    make_chart("a", profiles={"minimal": _requires("b")})
    make_chart("b", profiles={"minimal": _requires("a")})

    with pytest.raises(DependencyCycleError, match="dependency cycle detected"):
        LifecycleCompiler(chart_root).compile_cluster_test("a", "minimal")


def test_compile_rejects_an_unknown_chart_reference(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("a", profiles={"minimal": _requires("missing")})

    with pytest.raises(ChartManagerError):
        LifecycleCompiler(chart_root).compile_cluster_test("a", "minimal")


def test_compile_rejects_an_unknown_profile_reference(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("base")
    make_chart("a", profiles={"minimal": _requires("base:nope")})

    with pytest.raises(SpecError, match="unknown profile 'nope'"):
        LifecycleCompiler(chart_root).compile_cluster_test("a", "minimal")


def test_compile_accepts_a_valid_requires_graph(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("base")
    make_chart("app", profiles={"minimal": _requires("base")})

    plan = LifecycleCompiler(chart_root).compile_cluster_test("app", "minimal")

    assert [action.target.chart for action in plan.actions].count("base") >= 1
