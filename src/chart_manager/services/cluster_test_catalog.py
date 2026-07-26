"""Compose Helm charts with enabled live-cluster test configuration."""

from __future__ import annotations

from pathlib import Path

from chart_manager.plumbing.errors import SpecError
from chart_manager.services.chart_config import (
    CONFIG_FILENAME,
    CapabilityStatus,
    cluster_tests_status,
    load_optional_chart_manager_config,
    require_cluster_tests,
)
from chart_manager.services.domain.charts import (
    ChartRepository,
    ClusterTestChart,
)


class ClusterTestCatalog:
    """Load cluster-test capabilities without coupling Helm discovery to them."""

    def __init__(self, root: Path) -> None:
        """Anchor Helm and chart-manager configuration lookup at ``root``."""
        self.repository = ChartRepository(root)

    def get(self, name: str) -> ClusterTestChart:
        """Return ``name`` composed with its required, enabled cluster tests."""
        chart = self.repository.get(name)
        config = load_optional_chart_manager_config(chart.path / CONFIG_FILENAME)
        return ClusterTestChart(
            chart=chart,
            spec=require_cluster_tests(config, chart_name=chart.name),
        )

    def enabled_names(self) -> list[str]:
        """Return charts whose cluster-test capability is enabled.

        Present malformed configuration fails loudly rather than silently
        shrinking a CI matrix.
        """
        enabled: list[str] = []
        for name in self.repository.list_names():
            chart = self.repository.get(name)
            config = load_optional_chart_manager_config(chart.path / CONFIG_FILENAME)
            if cluster_tests_status(config) is CapabilityStatus.ENABLED:
                enabled.append(name)
        return enabled

    def value_paths(self, chart: ClusterTestChart, profile: str) -> list[Path]:
        """Resolve a profile's values files; every path must exist."""
        profile_spec = chart.spec.profile(profile)
        paths = [chart.path / value for value in profile_spec.values]
        missing = [path for path in paths if not path.exists()]
        if missing:
            rendered = ", ".join(str(path) for path in missing)
            raise SpecError(
                f"missing values file(s) for {chart.name}:{profile}: {rendered}"
            )
        return paths
