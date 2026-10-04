"""Compose Helm charts with enabled live-chart test configuration."""

from __future__ import annotations

from pathlib import Path

from chart_manager.plumbing.errors import SpecError
from chart_manager.shared.charts.chart import (
    ChartRepository,
    ChartUnderTest,
)
from chart_manager.shared.charts.lifecycle import (
    LIFECYCLE_FILENAME,
    CapabilityStatus,
    chart_test_status,
    load_optional_chart_lifecycle,
    require_chart_test,
    require_chart_test_profile,
    validate_chart_lifecycle_identity,
)


class ChartTestCatalog:
    """Load chart-test capabilities without coupling Helm discovery to them."""

    def __init__(self, root: Path, *, charts_dir: Path) -> None:
        """Anchor Helm and lifecycle-intent lookup at ``root``."""
        self.repository = ChartRepository(root, charts_dir=charts_dir)

    def get(self, name: str) -> ChartUnderTest:
        """Return ``name`` composed with its required, enabled chart tests."""
        chart = self.repository.get(name)
        lifecycle = load_optional_chart_lifecycle(chart.path / LIFECYCLE_FILENAME)
        if lifecycle is not None:
            validate_chart_lifecycle_identity(
                lifecycle,
                chart_name=chart.name,
                chart_directory=chart.path,
            )
        return ChartUnderTest(
            chart=chart,
            spec=require_chart_test(lifecycle, chart_name=chart.name),
        )

    def enabled_names(self) -> list[str]:
        """Return charts whose chart-test capability is enabled.

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
            if chart_test_status(lifecycle) is CapabilityStatus.ENABLED:
                enabled.append(name)
        return enabled

    def value_paths(self, chart: ChartUnderTest, profile: str) -> list[Path]:
        """Resolve a profile's values files; every path must exist."""
        profile_spec = require_chart_test_profile(chart.spec, profile)
        paths = [chart.path / value for value in profile_spec.values]
        missing = [path for path in paths if not path.exists()]
        if missing:
            rendered = ", ".join(str(path) for path in missing)
            raise SpecError(
                f"missing values file(s) for {chart.name}:{profile}: {rendered}"
            )
        return paths
