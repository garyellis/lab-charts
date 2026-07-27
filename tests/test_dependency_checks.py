"""InstallPlanService check resolution.

`deps checks` used to walk the install plan in the CLI and reach through
`service.repository` for each entry's profile. The traversal now belongs to
the service so a REST/RPC surface gets the same list without knowing the
repository exists.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.services.domain.charts import (
    ChartMetadata,
    ClusterTestChart,
    HelmChart,
)
from chart_manager.services.domain.cluster_tests import (
    ClusterCheckSpec,
    ClusterTestProfile,
    ClusterTestSpec,
)
from chart_manager.services.domain.install_plan import InstallPlanEntry
from chart_manager.services.install_plan import InstallPlanService


def _chart(name: str, profile: ClusterTestProfile) -> ClusterTestChart:
    return ClusterTestChart(
        chart=HelmChart(
            name=name,
            path=Path(f"/tmp/{name}"),
            metadata=ChartMetadata(name, "0.0.0", "application", ()),
        ),
        spec=ClusterTestSpec(profiles={"minimal": profile}, dependentTests=[]),
    )


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> InstallPlanService:
    """An InstallPlanService over an in-memory two-chart plan."""
    svc = InstallPlanService(tmp_path)
    charts = {
        "prometheus-operator": _chart(
            "prometheus-operator",
            ClusterTestProfile(helmTest=True, checks=[]),
        ),
        "alloy": _chart(
            "alloy",
            ClusterTestProfile(
                helmTest=True,
                checks=[ClusterCheckSpec(name="alloy-pods-ready", type="pod")],
            ),
        ),
    }
    monkeypatch.setattr(svc.catalog, "get", lambda name: charts[name])
    monkeypatch.setattr(
        svc.resolver,
        "install_plan",
        lambda _c, _p: [
            InstallPlanEntry(chart="prometheus-operator", profile="minimal"),
            InstallPlanEntry(chart="alloy", profile="minimal"),
        ],
    )
    return svc


def test_checks_for_includes_the_implicit_helm_test(service: InstallPlanService) -> None:
    assert [c.name for c in service.checks_for("alloy", "minimal")] == [
        "alloy-pods-ready",
        "helm-test",
    ]


def test_plan_checks_walks_the_whole_install_plan_in_order(
    service: InstallPlanService,
) -> None:
    plan = service.plan_checks("alloy", "minimal")

    assert [entry.chart for entry in plan] == ["prometheus-operator", "alloy"]
    assert [entry.profile for entry in plan] == ["minimal", "minimal"]
    # A profile with no declared checks still contributes its implicit one.
    assert [c.name for c in plan[0].checks] == ["helm-test"]
    assert [c.name for c in plan[1].checks] == ["alloy-pods-ready", "helm-test"]
