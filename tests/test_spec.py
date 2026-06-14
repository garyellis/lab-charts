from pathlib import Path

import pytest

from lab_charts.plumbing.spec import SpecError, load_test_spec


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
