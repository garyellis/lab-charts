"""Compose charts with enabled live-chart test configuration."""

from __future__ import annotations

from pathlib import Path

from chart_manager.plumbing.errors import SpecError
from chart_manager.shared.charts.chart import ChartUnderTest, chart_names, load_chart
from chart_manager.shared.charts.lifecycle import (
    CapabilityStatus,
    chart_test_status,
    require_chart_test,
    require_chart_test_profile,
)


class ChartTestCatalog:
    """Load the charts under one directory together with their chart tests."""

    def __init__(self, root: Path, *, charts_dir: Path) -> None:
        """Anchor chart lookup at ``root / charts_dir``."""
        self.charts_dir = root.resolve() / charts_dir

    def get(self, name: str) -> ChartUnderTest:
        """Return ``name`` composed with its required, enabled chart tests."""
        chart = load_chart(self.charts_dir / name)
        spec = require_chart_test(chart.lifecycle, chart_name=chart.name)
        return ChartUnderTest(chart=chart, spec=spec)

    def enabled_names(self) -> list[str]:
        """Return charts whose chart-test capability is enabled.

        Present malformed configuration fails loudly rather than silently
        shrinking a CI matrix.
        """
        return [
            name
            for name in chart_names(self.charts_dir)
            if chart_test_status(load_chart(self.charts_dir / name).lifecycle)
            is CapabilityStatus.ENABLED
        ]

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
