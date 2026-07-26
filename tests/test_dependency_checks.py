"""DependencyService check resolution.

`deps checks` used to walk the install plan in the CLI and reach through
`service.repository` for each entry's profile. The traversal now belongs to
the service so a REST/RPC surface gets the same list without knowing the
repository exists.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.services.dependencies import DependencyService
from chart_manager.services.domain.charts import (
    ChartMetadata,
    HelmChart,
    ManagedChart,
)
from chart_manager.services.domain.graph import PlanEntry
from chart_manager.services.domain.spec import CheckSpec, ProfileSpec
from chart_manager.services.domain.spec import TestSpec as _TestSpec


def _chart(name: str, profile: ProfileSpec) -> ManagedChart:
    return ManagedChart(
        chart=HelmChart(
            name=name,
            path=Path(f"/tmp/{name}"),
            metadata=ChartMetadata(name, "0.0.0", "application", ()),
        ),
        spec=_TestSpec(profiles={"minimal": profile}, reverse_tests=[]),
    )


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DependencyService:
    """A DependencyService over an in-memory two-chart plan (no chart tree on disk)."""
    svc = DependencyService(tmp_path)
    charts = {
        "prometheus-operator": _chart(
            "prometheus-operator",
            ProfileSpec(helm_test=True, checks=[]),
        ),
        "alloy": _chart(
            "alloy",
            ProfileSpec(
                helm_test=True,
                checks=[CheckSpec(name="alloy-pods-ready", type="pod")],
            ),
        ),
    }
    monkeypatch.setattr(svc.repository, "get_managed", lambda name: charts[name])
    monkeypatch.setattr(
        svc.resolver,
        "install_plan",
        lambda _c, _p: [
            PlanEntry(chart="prometheus-operator", profile="minimal"),
            PlanEntry(chart="alloy", profile="minimal"),
        ],
    )
    return svc


def test_checks_for_includes_the_implicit_helm_test(service: DependencyService) -> None:
    assert [c.name for c in service.checks_for("alloy", "minimal")] == [
        "alloy-pods-ready",
        "helm-test",
    ]


def test_plan_checks_walks_the_whole_install_plan_in_order(
    service: DependencyService,
) -> None:
    plan = service.plan_checks("alloy", "minimal")

    assert [entry.chart for entry in plan] == ["prometheus-operator", "alloy"]
    assert [entry.profile for entry in plan] == ["minimal", "minimal"]
    # A profile with no declared checks still contributes its implicit one.
    assert [c.name for c in plan[0].checks] == ["helm-test"]
    assert [c.name for c in plan[1].checks] == ["alloy-pods-ready", "helm-test"]
