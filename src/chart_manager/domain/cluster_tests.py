"""Compose Helm charts with enabled live-cluster test configuration."""

from __future__ import annotations

from pathlib import Path

from chart_manager.domain.charts import (
    ChartRepository,
    ClusterTestChart,
)
from chart_manager.domain.lifecycle_policy import (
    LIFECYCLE_FILENAME,
    CapabilityStatus,
    cluster_test_status,
    load_optional_chart_lifecycle,
    require_cluster_test,
    require_cluster_test_profile,
    validate_chart_lifecycle_identity,
)
from chart_manager.plumbing.errors import SpecError
from chart_manager.settings import DEFAULT_CHARTS_DIR


class ClusterTestCatalog:
    """Load cluster-test capabilities without coupling Helm discovery to them."""

    def __init__(self, root: Path, *, charts_dir: Path = DEFAULT_CHARTS_DIR) -> None:
        """Anchor Helm and lifecycle-intent lookup at ``root``."""
        self.repository = ChartRepository(root, charts_dir=charts_dir)

    def get(self, name: str) -> ClusterTestChart:
        """Return ``name`` composed with its required, enabled cluster tests."""
        chart = self.repository.get(name)
        lifecycle = load_optional_chart_lifecycle(chart.path / LIFECYCLE_FILENAME)
        if lifecycle is not None:
            validate_chart_lifecycle_identity(
                lifecycle,
                chart_name=chart.name,
                chart_directory=chart.path,
            )
        return ClusterTestChart(
            chart=chart,
            spec=require_cluster_test(lifecycle, chart_name=chart.name),
        )

    def enabled_names(self) -> list[str]:
        """Return charts whose cluster-test capability is enabled.

        Present malformed configuration fails loudly rather than silently
        shrinking a CI matrix.
        """
        enabled: list[str] = []
        for name in self.repository.list_names():
            chart = self.repository.get(name)
            lifecycle = load_optional_chart_lifecycle(chart.path / LIFECYCLE_FILENAME)
            if lifecycle is not None:
                validate_chart_lifecycle_identity(
                    lifecycle,
                    chart_name=chart.name,
                    chart_directory=chart.path,
                )
            if cluster_test_status(lifecycle) is CapabilityStatus.ENABLED:
                enabled.append(name)
        return enabled

    def value_paths(self, chart: ClusterTestChart, profile: str) -> list[Path]:
        """Resolve a profile's values files; every path must exist."""
        profile_spec = require_cluster_test_profile(chart.spec, profile)
        paths = [chart.path / value for value in profile_spec.values]
        missing = [path for path in paths if not path.exists()]
        if missing:
            rendered = ", ".join(str(path) for path in missing)
            raise SpecError(
                f"missing values file(s) for {chart.name}:{profile}: {rendered}"
            )
        return paths
