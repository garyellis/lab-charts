"""`install_plan` resolution and install ordering.

Asserted against synthetic chart trees, not the repo's own `charts/` -- see
tests/conftest.py. The real tree is exercised by a structural smoke test at
the bottom that survives new charts and new dependency edges.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from chart_manager.plumbing.errors import (
    CapabilityUnavailableError,
    ChartManagerError,
    ChartNotFoundError,
    DependencyCycleError,
    SpecError,
)
from chart_manager.plumbing.yaml_files import dump_yaml, parse_yaml
from chart_manager.shared.charts.install_plan import install_plan
from tests.conftest import CHARTS_DIR

from .conftest import REPO_ROOT, MakeChart


def _requires(*refs: str) -> dict[str, list[dict[str, str]]]:
    """Build a `requires:` list from "chart" or "chart:profile" shorthand."""
    parsed = []
    for ref in refs:
        chart, _, profile = ref.partition(":")
        parsed.append({"chart": chart, "profile": profile or "minimal"})
    return {"requires": parsed}


def test_install_plan_resolves_each_entry_requirements_first(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("prometheus-operator", profiles={"minimal": {"namespace": "operators"}})
    make_chart(
        "alloy",
        profiles={
            "minimal": {
                **_requires("prometheus-operator"),
                "namespace": "monitoring",
                "values": ["values.yaml", "values-ci.yaml"],
                "timeout": "7m",
            }
        },
    )

    plan = install_plan(chart_root / CHARTS_DIR, "alloy", "minimal")

    charts = chart_root / "charts"
    assert [
        (entry.chart.name, entry.chart.path, entry.profile, entry.namespace, entry.values,
         entry.spec.timeout)
        for entry in plan
    ] == [
        ("prometheus-operator", charts / "prometheus-operator", "minimal", "operators",
         (charts / "prometheus-operator/values.yaml",), "10m"),
        ("alloy", charts / "alloy", "minimal", "monitoring",
         (charts / "alloy/values.yaml", charts / "alloy/values-ci.yaml"), "7m"),
    ]
    assert plan[-1].spec.requires[0].chart == "prometheus-operator"


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

    plan = install_plan(chart_root / CHARTS_DIR, "alloy", "ui-e2e")

    # Requirements are planned in declaration order, nested ones first, target last.
    assert [(entry.chart.name, entry.profile) for entry in plan] == [
        ("prometheus-operator", "minimal"),
        ("istio-base", "minimal"),
        ("mimir-distributed", "minimal"),
        ("loki", "minimal"),
        ("tempo", "minimal"),
        ("grafana", "with-deps"),
        ("alloy", "ui-e2e"),
    ]


def test_a_shared_dependency_is_planned_once_before_both_dependents(
    chart_root: Path, make_chart: MakeChart
) -> None:
    """Diamond: both branches require `base`, which must appear exactly once."""
    make_chart("base")
    make_chart("left", profiles={"minimal": _requires("base")})
    make_chart("right", profiles={"minimal": _requires("base")})
    make_chart("app", profiles={"minimal": _requires("left", "right")})

    plan = install_plan(chart_root / CHARTS_DIR, "app", "minimal")

    assert [entry.chart.name for entry in plan] == ["base", "left", "right", "app"]


def test_the_same_chart_under_two_profiles_is_not_deduped(
    chart_root: Path, make_chart: MakeChart
) -> None:
    """Dedupe keys on (chart, profile), so two profiles of one chart both install."""
    make_chart("base", profiles={"minimal": {}, "full": {}})
    make_chart("app", profiles={"minimal": _requires("base:minimal", "base:full")})

    plan = install_plan(chart_root / CHARTS_DIR, "app", "minimal")

    assert [(entry.chart.name, entry.profile) for entry in plan] == [
        ("base", "minimal"),
        ("base", "full"),
        ("app", "minimal"),
    ]


def _disable(chart: Path) -> None:
    path = chart / "chart-lifecycle.yaml"
    config = parse_yaml(path.read_text())
    config["spec"]["enabled"] = False
    path.write_text(dump_yaml(config), encoding="utf-8")


@pytest.mark.parametrize(
    ("app", "prepare", "error", "match"),
    [
        (_requires("b"), lambda make: make("b", profiles={"minimal": _requires("app")}),
         DependencyCycleError, "dependency cycle detected: app:minimal -> b:minimal -> app"),
        (_requires("missing"), lambda _make: None, ChartNotFoundError, "missing"),
        (_requires("b:nope"), lambda make: make("b"), SpecError, "unknown profile 'nope'"),
        (_requires("b"), lambda make: _disable(make("b")), CapabilityUnavailableError,
         "ChartLifecycle is disabled for chart 'b'"),
        (_requires("b"), lambda make: (make("b") / "chart-lifecycle.yaml").unlink(),
         CapabilityUnavailableError, r"no chartTest configuration in chart-lifecycle\.yaml"),
        (_requires("b"), lambda make: (make("b") / "values.yaml").unlink(), SpecError,
         "missing values file"),
    ],
    ids=["cycle", "unknown-chart", "unknown-profile", "disabled", "unmanaged", "missing-values"],
)
def test_install_plan_rejects_a_requirement_it_cannot_resolve(
    chart_root: Path,
    make_chart: MakeChart,
    app: dict[str, object],
    prepare: Callable[[MakeChart], object],
    error: type[ChartManagerError],
    match: str,
) -> None:
    make_chart("app", profiles={"minimal": app})
    prepare(make_chart)

    with pytest.raises(error, match=match):
        install_plan(chart_root / CHARTS_DIR, "app", "minimal")


def test_the_repo_dependency_graph_resolves() -> None:
    """Smoke test over the real charts/ tree: structure, not inventory."""
    plan = install_plan(REPO_ROOT / CHARTS_DIR, "alloy", "ui-e2e")

    assert (plan[-1].chart.name, plan[-1].profile) == ("alloy", "ui-e2e")
    keys = [(entry.chart.name, entry.profile) for entry in plan]
    assert len(keys) == len(set(keys)), "install plan must not repeat a chart:profile"
