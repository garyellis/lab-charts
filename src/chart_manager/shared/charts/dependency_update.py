"""Bring a chart's Helm dependencies up to date when they are stale.

Separate from `dependencies.py` because `chart validate`'s package root imports that
module and must load no adapter.
"""

from __future__ import annotations

from pathlib import Path

from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.errors import SpecError
from chart_manager.shared.charts.chart import load_chart_metadata
from chart_manager.shared.charts.dependencies import deps_are_fresh

# Bounds `helm dependency update` when no tool or command timeout is configured.
DEPENDENCY_UPDATE_TIMEOUT = 600.0


def ensure_dependencies(helm: Helm, chart_path: Path) -> None:
    """Run `helm dependency update` when the chart declares dependencies and they are stale."""
    try:
        declared = load_chart_metadata(chart_path / "Chart.yaml").dependencies
    except SpecError:
        # Helm itself reports a malformed chart when it renders or installs it.
        return
    if declared and not deps_are_fresh(chart_path):
        update_dependencies(helm, chart_path)


def update_dependencies(helm: Helm, chart_path: Path) -> None:
    """Run `helm dependency update` within Helm's timeout, else `DEPENDENCY_UPDATE_TIMEOUT`."""
    timeout = DEPENDENCY_UPDATE_TIMEOUT if helm.timeout is None else helm.timeout
    helm.dependency_update(chart_path, timeout=timeout)
