from pathlib import Path

import pytest

from chart_manager.plumbing.spec import (
    CheckSpec,
    ProfileSpec,
    SpecError,
    load_test_spec,
)


def test_load_test_spec_accepts_chart_refs() -> None:
    spec = load_test_spec(Path("charts/alloy/test-spec.yaml"))

    minimal = spec.profile("minimal")

    assert minimal.requires[0].chart == "prometheus-operator"
    assert minimal.requires[0].profile == "minimal"
    assert minimal.helm_test is True
    assert minimal.checks[0].name == "alloy-pods-ready"


def test_unknown_profile_raises_spec_error() -> None:
    spec = load_test_spec(Path("charts/alloy/test-spec.yaml"))

    with pytest.raises(SpecError):
        spec.profile("missing")


# ----- ProfileSpec.effective_checks -----------------------------------------
#
# The implicit helm-test check used to be synthesized in `cli/main.py`'s
# `deps checks` handler. It is a domain rule with one correct answer, so it
# lives on the model and every surface sees the same list.


def test_effective_checks_appends_implicit_helm_test() -> None:
    profile = ProfileSpec(helm_test=True, checks=[CheckSpec(name="pods-ready", type="pod")])

    checks = profile.effective_checks()

    assert [c.name for c in checks] == ["pods-ready", "helm-test"]
    assert checks[-1].type == "helm-test"
    assert checks[-1].description == "Run Helm test hooks for the release."


def test_effective_checks_omits_implicit_check_when_helm_test_disabled() -> None:
    profile = ProfileSpec(helm_test=False, checks=[CheckSpec(name="pods-ready", type="pod")])

    assert [c.name for c in profile.effective_checks()] == ["pods-ready"]


def test_effective_checks_does_not_duplicate_an_explicit_helm_test_check() -> None:
    # An explicitly declared helm-test check wins: the profile author gets
    # to name and describe it.
    explicit = CheckSpec(name="my-smoke", type="helm-test", description="custom")
    profile = ProfileSpec(helm_test=True, checks=[explicit])

    assert profile.effective_checks() == [explicit]


def test_effective_checks_returns_a_fresh_list() -> None:
    # Callers mutate the returned list (the CLI used to); it must not
    # alias the model's own `checks`.
    profile = ProfileSpec(helm_test=True, checks=[])

    profile.effective_checks().append(CheckSpec(name="x"))

    assert profile.checks == []
