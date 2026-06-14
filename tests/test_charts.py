from pathlib import Path

from lab_charts.plumbing.charts import ChartRepository


def test_list_charts_discovers_wrappers() -> None:
    repository = ChartRepository(Path("."))

    assert repository.list_names() == [
        "alloy",
        "grafana",
        "kube-state-metrics",
        "loki",
        "mimir-distributed",
        "node-exporter",
        "prometheus-operator",
        "tempo",
    ]


def test_value_paths_are_chart_relative() -> None:
    repository = ChartRepository(Path("."))
    chart = repository.get("prometheus-operator")

    paths = repository.value_paths(chart, "minimal")

    assert paths == [
        Path(".").resolve() / "charts/prometheus-operator/values.yaml",
        Path(".").resolve() / "charts/prometheus-operator/values-ci.yaml",
    ]
