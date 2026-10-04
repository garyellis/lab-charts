from pathlib import Path

import pytest
from pydantic import ValidationError

from chart_manager.api.v1alpha1.chart_lifecycle import ChartTestProfile, ChartTestSpec
from chart_manager.plumbing.errors import SpecError
from chart_manager.shared.charts.lifecycle import (
    load_chart_lifecycle,
    require_chart_test,
    require_chart_test_profile,
)

from .conftest import cli


def _alloy_spec() -> ChartTestSpec:
    lifecycle = load_chart_lifecycle(Path("charts/alloy/chart-lifecycle.yaml"))
    return require_chart_test(lifecycle, chart_name="alloy")


def test_load_test_spec_accepts_chart_refs() -> None:
    spec = _alloy_spec()

    minimal = require_chart_test_profile(spec, "minimal")

    assert minimal.requires[0].chart == "prometheus-operator"
    assert minimal.requires[0].profile == "minimal"
    assert minimal.helm_test is True


def test_unknown_profile_raises_spec_error() -> None:
    spec = _alloy_spec()

    with pytest.raises(SpecError):
        require_chart_test_profile(spec, "missing")


def test_dependent_tests_is_the_only_authored_reverse_target_field() -> None:
    spec = ChartTestSpec.model_validate(
        {
            "profiles": {"minimal": {"namespace": "default"}},
            "dependentTests": [{"chart": "grafana", "profile": "with-deps"}],
        }
    )

    assert [(ref.chart, ref.profile) for ref in spec.dependent_tests] == [
        ("grafana", "with-deps")
    ]

    with pytest.raises(ValidationError, match="reverseTests"):
        ChartTestSpec.model_validate(
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
    assert ChartTestProfile(namespace="default").helm_test is True


def test_cluster_test_profile_accepts_disabled_helm_tests() -> None:
    assert ChartTestProfile(namespace="default", helmTest=False).helm_test is False


def test_cluster_test_profile_rejects_removed_checks_configuration() -> None:
    with pytest.raises(ValidationError, match="checks"):
        ChartTestProfile.model_validate(
            {
                "namespace": "default",
                "checks": [{"name": "pods-ready", "type": "helm-test"}],
            }
        )
