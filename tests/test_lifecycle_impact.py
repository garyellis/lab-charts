"""Spec-derived validation and cluster-test impact analysis."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from chart_manager.services.lifecycle import (
    ImpactReasonCode,
    LifecycleImpactService,
)

from .conftest import MakeChart


def _with_validation(chart: Path, *, environments: tuple[str, ...] = ("dev",)) -> None:
    lifecycle = yaml.safe_load((chart / "chart-lifecycle.yaml").read_text())
    lifecycle["spec"]["validation"] = {
        "releaseName": chart.name,
        "namespaceTemplate": "lab-${env}",
        "environments": {
            environment: {"values": ["values.yaml", f"values-{environment}.yaml"]}
            for environment in environments
        },
        "triggers": {
            "values.yaml": list(environments),
            **{
                f"values-{environment}.yaml": [environment]
                for environment in environments
            },
        },
    }
    for environment in environments:
        (chart / f"values-{environment}.yaml").write_text("{}\n")
    (chart / "chart-lifecycle.yaml").write_text(yaml.safe_dump(lifecycle))


def _with_dependent_test(
    chart: Path,
    *,
    dependent_chart: str,
    dependent_profile: str,
) -> None:
    lifecycle = yaml.safe_load((chart / "chart-lifecycle.yaml").read_text())
    lifecycle["spec"]["clusterTest"]["dependentTests"] = [
        {"chart": dependent_chart, "profile": dependent_profile}
    ]
    (chart / "chart-lifecycle.yaml").write_text(yaml.safe_dump(lifecycle))


def test_ordinary_chart_change_selects_validation_cluster_and_declared_dependent(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    source = make_chart("source")
    _with_validation(source, environments=("dev", "prod"))
    target = make_chart("consumer", profiles={"minimal": {}, "full": {}})
    _with_validation(target)
    _with_dependent_test(
        source,
        dependent_chart="consumer",
        dependent_profile="full",
    )

    impact = LifecycleImpactService(chart_root).analyze(
        ["charts/source/values-dev.yaml"]
    )

    assert [(case.chart, case.environment) for case in impact.validation] == [
        ("source", "dev")
    ]
    assert [(case.chart, case.profile) for case in impact.cluster_tests] == [
        ("consumer", "full"),
        ("source", "minimal"),
    ]
    assert impact.cluster_tests[0].reasons[0].code is (
        ImpactReasonCode.DECLARED_DEPENDENT_TEST
    )
    assert impact.cluster_tests[1].reasons[0].code is ImpactReasonCode.CHART_CHANGE


def test_chart_lifecycle_change_selects_all_validation_environments_and_cluster_test(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("app")
    _with_validation(chart, environments=("dev", "prod"))

    impact = LifecycleImpactService(chart_root).analyze(
        ["charts/app/chart-lifecycle.yaml"],
    )

    assert [(case.chart, case.environment) for case in impact.validation] == [
        ("app", "dev"),
        ("app", "prod"),
    ]
    assert [(case.chart, case.profile) for case in impact.cluster_tests] == [
        ("app", "minimal"),
    ]


def test_shared_runtime_change_fans_out_every_enabled_cluster_test_with_reasons(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("alpha")
    make_chart("beta")

    impact = LifecycleImpactService(chart_root).analyze(
        ["charts/istio-base/templates/crd.yaml"],
    )

    assert [(case.chart, case.profile) for case in impact.cluster_tests] == [
        ("alpha", "minimal"),
        ("beta", "minimal"),
    ]
    assert all(
        case.reasons[0].code is ImpactReasonCode.CLUSTER_SAFETY_FANOUT
        for case in impact.cluster_tests
    )
    assert all("istio-base" in case.reasons[0].detail for case in impact.cluster_tests)


def test_local_bootstrap_chart_change_fans_out_without_cni_knowledge(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("alpha")
    make_chart("beta")
    bootstrap = chart_root / "platform/network"
    bootstrap.mkdir(parents=True)
    (bootstrap / "Chart.yaml").write_text(
        "apiVersion: v2\nname: network\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    config = chart_root / ".chart-manager/local-cluster.yaml"
    config.parent.mkdir()
    config.write_text(
        """
apiVersion: local.chartmanager.io/v1alpha1
kind: LocalCluster
metadata: {name: default}
spec:
  cluster: {config: kind-config.yaml}
  bootstrap:
    releases:
      - type: local
        name: network
        chart: platform/network
        namespace: kube-system
        values: []
        timeout: 5m
""".lstrip(),
        encoding="utf-8",
    )

    impact = LifecycleImpactService(chart_root).analyze(
        ["platform/network/templates/daemonset.yaml"],
    )

    assert [(case.chart, case.profile) for case in impact.cluster_tests] == [
        ("alpha", "minimal"),
        ("beta", "minimal"),
    ]
    assert all(
        "LocalCluster bootstrap prerequisite" in case.reasons[0].detail
        for case in impact.cluster_tests
    )


def test_safety_fanout_unions_declared_dependent_profiles_from_chart_changes(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    source = make_chart("source")
    make_chart("consumer", profiles={"minimal": {}, "full": {}})
    _with_dependent_test(
        source,
        dependent_chart="consumer",
        dependent_profile="full",
    )

    impact = LifecycleImpactService(chart_root).analyze(
        ["kind-config.yaml", "charts/source/values.yaml"],
    )

    assert [(case.chart, case.profile) for case in impact.cluster_tests] == [
        ("consumer", "full"),
        ("consumer", "minimal"),
        ("source", "minimal"),
    ]
    full = impact.cluster_tests[0]
    assert [reason.code for reason in full.reasons] == [
        ImpactReasonCode.DECLARED_DEPENDENT_TEST
    ]
    source_default = impact.cluster_tests[-1]
    assert {reason.code for reason in source_default.reasons} == {
        ImpactReasonCode.CHART_CHANGE,
        ImpactReasonCode.CLUSTER_SAFETY_FANOUT,
    }


def test_tool_workflow_and_chart_manager_rules_are_typed_safety_fanout(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("app")

    for changed_file in (
        "src/chart_manager/services/ci.py",
        "kind-config.yaml",
        ".mise.toml",
            "pyproject.toml",
            "uv.lock",
            ".github/workflows/ci.yaml",
            ".chart-manager/local-cluster.yaml",
        ):
        impact = LifecycleImpactService(chart_root).analyze([changed_file])
        assert [(case.chart, case.profile) for case in impact.cluster_tests] == [
            ("app", "minimal")
        ]
        reason = impact.cluster_tests[0].reasons[0]
        assert reason.code is ImpactReasonCode.CLUSTER_SAFETY_FANOUT
        assert reason.changed_file == Path(changed_file)


def test_repository_policy_change_uses_existing_validation_safety_fanout(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    alpha = make_chart("alpha")
    beta = make_chart("beta")
    _with_validation(alpha, environments=("dev", "prod"))
    _with_validation(beta)

    impact = LifecycleImpactService(chart_root).analyze(
        ["policies/require-resources.yaml"],
    )

    assert [(case.chart, case.environment) for case in impact.validation] == [
        ("alpha", "dev"),
        ("alpha", "prod"),
        ("beta", "dev"),
    ]
    assert all(
        case.reasons[0].code is ImpactReasonCode.REPOSITORY_POLICY
        for case in impact.validation
    )


def test_impact_is_deterministic_deduplicated_and_json_serializable(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    app = make_chart("app")
    _with_validation(app)
    changes = [
        "charts/app/values.yaml",
        "charts/app/values.yaml",
        "README.md",
    ]

    impact = LifecycleImpactService(chart_root).analyze(changes)
    projected = impact.to_dict()

    assert projected["changedFiles"] == [
        "README.md",
        "charts/app/values.yaml",
    ]
    assert len(projected["validationSelection"]) == 1
    assert len(projected["clusterTestMatrix"]) == 1
    assert projected["apiVersion"] == "lifecycle.chartmanager.io/v1alpha1"
    assert projected["kind"] == "LifecycleImpact"
    assert json.loads(json.dumps(projected)) == projected


def test_unrelated_non_chart_change_selects_no_lifecycle_work(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    app = make_chart("app")
    _with_validation(app)

    impact = LifecycleImpactService(chart_root).analyze(["docs/architecture.md"])

    assert impact.validation == ()
    assert impact.cluster_tests == ()
