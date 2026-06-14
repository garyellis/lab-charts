from pathlib import Path

import pytest

from chart_manager.plumbing.charts import ChartRepository
from chart_manager.plumbing.errors import DependencyCycleError
from chart_manager.plumbing.graph import DependencyResolver


def test_install_plan_orders_requirements_before_target() -> None:
    resolver = DependencyResolver(ChartRepository(Path(".")))

    plan = resolver.install_plan("alloy", "minimal")

    assert [(entry.chart, entry.target) for entry in plan] == [
        ("prometheus-operator", False),
        ("alloy", True),
    ]


def test_install_plan_expands_nested_profiles() -> None:
    resolver = DependencyResolver(ChartRepository(Path(".")))

    plan = resolver.install_plan("grafana", "with-deps")

    assert [entry.chart for entry in plan] == [
        "mimir-distributed",
        "loki",
        "tempo",
        "grafana",
    ]
    assert plan[-1].target is True


def test_alloy_ui_e2e_installs_grafana_stack_then_alloy() -> None:
    resolver = DependencyResolver(ChartRepository(Path(".")))

    plan = resolver.install_plan("alloy", "ui-e2e")

    assert [entry.chart for entry in plan] == [
        "prometheus-operator",
        "mimir-distributed",
        "loki",
        "tempo",
        "grafana",
        "alloy",
    ]
    assert plan[-1].profile == "ui-e2e"
    assert plan[-1].target is True


def test_cycle_detection(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    for name, required in [("a", "b"), ("b", "a")]:
        chart = charts / name
        chart.mkdir(parents=True)
        (chart / "Chart.yaml").write_text(f"apiVersion: v2\nname: {name}\n", encoding="utf-8")
        (chart / "values.yaml").write_text("", encoding="utf-8")
        (chart / "test-spec.yaml").write_text(
            f"""
version: 1
profiles:
  minimal:
    requires:
      - chart: {required}
        profile: minimal
    values:
      - values.yaml
    helmTest: false
reverseTests: []
""",
            encoding="utf-8",
        )

    resolver = DependencyResolver(ChartRepository(tmp_path))

    with pytest.raises(DependencyCycleError):
        resolver.install_plan("a", "minimal")
