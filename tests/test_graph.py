"""DependencyResolver install ordering.

Asserted against synthetic chart trees, not the repo's own `charts/` -- see
tests/conftest.py. The real tree is exercised by a structural smoke test at
the bottom that survives new charts and new dependency edges.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.plumbing.errors import DependencyCycleError
from chart_manager.services.domain.charts import ChartRepository
from chart_manager.services.domain.graph import DependencyResolver, PlanEntry

from .conftest import REPO_ROOT, MakeChart


def _requires(*refs: str) -> dict[str, list[dict[str, str]]]:
    """Build a `requires:` list from "chart" or "chart:profile" shorthand."""
    parsed = []
    for ref in refs:
        chart, _, profile = ref.partition(":")
        parsed.append({"chart": chart, "profile": profile or "minimal"})
    return {"requires": parsed}


def test_install_plan_orders_requirements_before_target(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("prometheus-operator")
    make_chart("alloy", profiles={"minimal": _requires("prometheus-operator")})

    plan = DependencyResolver(ChartRepository(chart_root)).install_plan("alloy", "minimal")

    assert [(entry.chart, entry.target) for entry in plan] == [
        ("prometheus-operator", False),
        ("alloy", True),
    ]


def test_install_plan_expands_nested_profiles(chart_root: Path, make_chart: MakeChart) -> None:
    for name in ("istio-base", "mimir-distributed", "loki", "tempo"):
        make_chart(name)
    make_chart(
        "grafana",
        profiles={
            "with-deps": _requires("istio-base", "mimir-distributed", "loki", "tempo"),
        },
    )

    plan = DependencyResolver(ChartRepository(chart_root)).install_plan("grafana", "with-deps")

    # Requirements are planned in declaration order, target last.
    assert [entry.chart for entry in plan] == [
        "istio-base",
        "mimir-distributed",
        "loki",
        "tempo",
        "grafana",
    ]
    assert plan[-1].target is True


def test_alloy_ui_e2e_installs_grafana_stack_then_alloy(
    chart_root: Path, make_chart: MakeChart
) -> None:
    for name in ("prometheus-operator", "istio-base", "mimir-distributed", "loki", "tempo"):
        make_chart(name)
    make_chart(
        "grafana",
        profiles={
            "with-deps": _requires("istio-base", "mimir-distributed", "loki", "tempo"),
        },
    )
    make_chart(
        "alloy",
        profiles={"ui-e2e": _requires("prometheus-operator", "grafana:with-deps")},
    )

    plan = DependencyResolver(ChartRepository(chart_root)).install_plan("alloy", "ui-e2e")

    assert [entry.chart for entry in plan] == [
        "prometheus-operator",
        "istio-base",
        "mimir-distributed",
        "loki",
        "tempo",
        "grafana",
        "alloy",
    ]
    assert plan[-1].profile == "ui-e2e"
    assert plan[-1].target is True


def test_a_shared_dependency_is_planned_once_before_both_dependents(
    chart_root: Path, make_chart: MakeChart
) -> None:
    """Diamond: both branches require `base`, which must appear exactly once."""
    make_chart("base")
    make_chart("left", profiles={"minimal": _requires("base")})
    make_chart("right", profiles={"minimal": _requires("base")})
    make_chart("app", profiles={"minimal": _requires("left", "right")})

    plan = DependencyResolver(ChartRepository(chart_root)).install_plan("app", "minimal")

    assert [entry.chart for entry in plan] == ["base", "left", "right", "app"]


def test_the_same_chart_under_two_profiles_is_not_deduped(
    chart_root: Path, make_chart: MakeChart
) -> None:
    """Dedupe keys on (chart, profile), so two profiles of one chart both install."""
    make_chart("base", profiles={"minimal": {}, "full": {}})
    make_chart("app", profiles={"minimal": _requires("base:minimal", "base:full")})

    plan = DependencyResolver(ChartRepository(chart_root)).install_plan("app", "minimal")

    assert [(entry.chart, entry.profile) for entry in plan] == [
        ("base", "minimal"),
        ("base", "full"),
        ("app", "minimal"),
    ]


def test_cycle_detection(chart_root: Path, make_chart: MakeChart) -> None:
    make_chart("a", profiles={"minimal": _requires("b")})
    make_chart("b", profiles={"minimal": _requires("a")})

    resolver = DependencyResolver(ChartRepository(chart_root))

    with pytest.raises(DependencyCycleError):
        resolver.install_plan("a", "minimal")


def test_the_repo_dependency_graph_resolves() -> None:
    """Smoke test over the real charts/ tree: structure, not inventory."""
    plan = DependencyResolver(ChartRepository(REPO_ROOT)).install_plan("alloy", "ui-e2e")

    assert plan[-1] == PlanEntry("alloy", "ui-e2e", target=True)
    keys = [(entry.chart, entry.profile) for entry in plan]
    assert len(keys) == len(set(keys)), "install plan must not repeat a chart:profile"
