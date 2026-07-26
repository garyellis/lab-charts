from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from chart_manager.cli.main import app
from chart_manager.services.chart_config import (
    load_chart_manager_config,
    require_cluster_tests,
)
from chart_manager.services.domain.cluster_tests import (
    ClusterCheckSpec,
    ClusterTestProfile,
    ClusterTestSpec,
    SpecError,
)


def _alloy_spec() -> ClusterTestSpec:
    config = load_chart_manager_config(Path("charts/alloy/chart-manager.yaml"))
    return require_cluster_tests(config, chart_name="alloy")


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
