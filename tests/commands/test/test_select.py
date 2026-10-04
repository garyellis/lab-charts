"""`test.select()`: which chart tests a change set, an explicit list or `--all` calls for."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.commands import test
from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.yaml_files import dump_yaml, parse_yaml
from tests.conftest import MakeChart, workspace_for


def _select(root: Path, changes: list[str] | None, charts: tuple[str, ...] = ()) -> test.Selection:
    workspace = workspace_for(root, fanout={"chartTest": ["kind-config.yaml"]})
    return test.select(changes, workspace=workspace, charts=charts)


def _picked(selection: test.Selection) -> list[tuple[str, str]]:
    return [(entry.chart, entry.profile) for entry in selection.tests]


def test_a_changed_chart_file_selects_that_chart_at_its_default_profile(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha")
    make_chart("beta")

    selection = _select(chart_root, ["charts/alpha/values.yaml"])

    assert _picked(selection) == [("alpha", "minimal")]
    (reason,) = selection.tests[0].reasons
    assert reason == test.Reason(
        test.ReasonCode.CHART_CHANGE,
        Path("charts/alpha/values.yaml"),
        "changed file belongs to alpha, which has chart tests enabled",
    )


def _depends_on(chart: Path, *, target: str, profile: str) -> None:
    path = chart / "chart-lifecycle.yaml"
    lifecycle = parse_yaml(path.read_text())
    lifecycle["spec"]["chartTest"]["dependentTests"] = [{"chart": target, "profile": profile}]
    path.write_text(dump_yaml(lifecycle))


def test_a_changed_chart_also_selects_its_dependent_tests_at_their_declared_profile(
    chart_root: Path, make_chart: MakeChart
) -> None:
    source = make_chart("source")
    make_chart("consumer", profiles={"minimal": {}, "full": {}})
    _depends_on(source, target="consumer", profile="full")

    selection = _select(chart_root, ["charts/source/values.yaml"])

    assert _picked(selection) == [("consumer", "full"), ("source", "minimal")]
    assert selection.tests[0].reasons == (
        test.Reason(
            test.ReasonCode.DECLARED_DEPENDENT_TEST,
            Path("charts/source/values.yaml"),
            "source declares dependent test consumer:full",
        ),
    )
    assert selection.spec_errors == ()


def test_a_dependent_test_naming_an_unknown_profile_is_a_spec_error(
    chart_root: Path, make_chart: MakeChart
) -> None:
    source = make_chart("source")
    make_chart("consumer")
    _depends_on(source, target="consumer", profile="nope")

    selection = _select(chart_root, ["charts/source/values.yaml"])

    assert _picked(selection) == [("source", "minimal")]
    assert selection.spec_errors == (
        "source dependentTests consumer:nope: unknown profile 'nope'. available profiles: minimal",
    )


def test_a_change_matching_the_chart_test_fanout_selects_every_enabled_chart(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha")
    make_chart("beta")
    _disable(make_chart("off"))

    selection = _select(chart_root, ["kind-config.yaml", "README.md"])

    assert _picked(selection) == [("alpha", "minimal"), ("beta", "minimal")]
    assert {entry.reasons for entry in selection.tests} == {
        (
            test.Reason(
                test.ReasonCode.CHART_TEST_FANOUT,
                Path("kind-config.yaml"),
                "workspace chart-test fanout matched kind-config.yaml",
            ),
        )
    }


def test_a_chart_without_the_default_profile_runs_its_first_profile_in_sorted_order(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha", profiles={"smoke": {}, "full": {}})

    assert _picked(_select(chart_root, ["charts/alpha/values.yaml"])) == [("alpha", "full")]


def test_no_changes_selects_every_enabled_chart_at_its_default_profile(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha", profiles={"smoke": {}, "full": {}})
    make_chart("beta", profiles={"minimal": {}, "full": {}})
    _disable(make_chart("off"))

    selection = _select(chart_root, None)

    assert _picked(selection) == [("alpha", "full"), ("beta", "minimal")]
    assert all(entry.reasons == () for entry in selection.tests)


def test_explicit_charts_select_exactly_those_charts_whatever_changed(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha")
    make_chart("beta", profiles={"smoke": {}})
    make_chart("gamma")

    selection = _select(chart_root, ["kind-config.yaml"], charts=("beta", "alpha", "beta"))

    assert _picked(selection) == [("alpha", "minimal"), ("beta", "smoke")]


def test_explicit_charts_reject_every_unknown_and_disabled_chart_at_once(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("enabled")
    _disable(make_chart("disabled"))

    with pytest.raises(SpecError) as caught:
        _select(chart_root, None, charts=("missing", "disabled", "enabled"))

    assert str(caught.value) == (
        "invalid chart-test request: unknown chart(s): missing; "
        "chart(s) without enabled chart tests: disabled"
    )


def _disable(chart: Path) -> None:
    path = chart / "chart-lifecycle.yaml"
    lifecycle = parse_yaml(path.read_text())
    lifecycle["spec"]["chartTest"]["enabled"] = False
    path.write_text(dump_yaml(lifecycle))


def test_a_change_to_a_shared_chart_selects_every_enabled_chart(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha")
    make_chart("beta")
    workspace = workspace_for(chart_root, chartTest={"sharedCharts": ["istio-base"]})

    selection = test.select(["charts/istio-base/templates/crd.yaml"], workspace=workspace)

    assert _picked(selection) == [("alpha", "minimal"), ("beta", "minimal")]
    assert {entry.reasons[0].detail for entry in selection.tests} == {
        "istio-base is a shared chart used by every chart test"
    }


def test_a_change_to_a_local_cluster_bootstrap_chart_selects_every_enabled_chart(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha")
    (chart_root / "platform/network").mkdir(parents=True)
    (chart_root / "platform/network/Chart.yaml").write_text(
        "apiVersion: v2\nname: network\nversion: 1.0.0\n"
    )
    (chart_root / ".chart-manager/local-cluster.yaml").write_text(
        dump_yaml(
            {
                "apiVersion": "chartmanager.io/v1alpha1",
                "kind": "LocalCluster",
                "metadata": {"name": "default"},
                "spec": {
                    "cluster": {"config": "kind-config.yaml"},
                    "bootstrap": {
                        "releases": [
                            {
                                "type": "local",
                                "name": "network",
                                "chart": "platform/network",
                                "namespace": "kube-system",
                                "values": [],
                                "timeout": "5m",
                            }
                        ]
                    },
                },
            }
        )
    )

    selection = _select(chart_root, ["platform/network/templates/daemonset.yaml"])

    assert _picked(selection) == [("alpha", "minimal")]
    assert selection.tests[0].reasons[0].detail == (
        "platform/network is part of the LocalCluster every chart test runs on"
    )


def test_a_fanout_change_and_a_chart_change_together_also_select_dependent_profiles(
    chart_root: Path, make_chart: MakeChart
) -> None:
    source = make_chart("source")
    make_chart("consumer", profiles={"minimal": {}, "full": {}})
    _depends_on(source, target="consumer", profile="full")

    selection = _select(chart_root, ["kind-config.yaml", "charts/source/values.yaml"])

    assert _picked(selection) == [
        ("consumer", "full"),
        ("consumer", "minimal"),
        ("source", "minimal"),
    ]
    assert {reason.code for reason in selection.tests[-1].reasons} == {
        test.ReasonCode.CHART_CHANGE,
        test.ReasonCode.CHART_TEST_FANOUT,
    }
