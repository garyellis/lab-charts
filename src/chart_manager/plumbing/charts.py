"""Chart discovery and loading from the repo's `charts/` directory."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.errors import ChartNotFoundError, SpecError
from chart_manager.plumbing.spec import TestSpec, load_test_spec, load_yaml_file


@dataclass(frozen=True)
class Chart:
    """A loaded chart: parsed Chart.yaml plus its test spec."""

    name: str
    path: Path
    chart_yaml: dict[str, object]
    spec: TestSpec


class ChartRepository:
    """Look up charts under `<root>/charts/` by directory name."""

    def __init__(self, root: Path) -> None:
        """Anchor the repository at the resolved repo root."""
        self.root = root.resolve()
        self.charts_dir = self.root / "charts"

    def list_names(self) -> list[str]:
        """Return sorted names of chart directories containing a Chart.yaml."""
        if not self.charts_dir.exists():
            return []
        names = [
            path.name
            for path in self.charts_dir.iterdir()
            if path.is_dir() and (path / "Chart.yaml").exists()
        ]
        return sorted(names)

    def get(self, name: str) -> Chart:
        """Load a chart by name; requires a test-spec.yaml to be present."""
        path = self.charts_dir / name
        chart_yaml_path = path / "Chart.yaml"
        if not chart_yaml_path.exists():
            raise ChartNotFoundError(f"chart not found: {name}")
        chart_yaml = load_yaml_file(chart_yaml_path)
        chart_name = chart_yaml.get("name")
        if chart_name != name:
            raise SpecError(
                f"{chart_yaml_path} name '{chart_name}' does not match directory '{name}'"
            )
        return Chart(
            name=name,
            path=path,
            chart_yaml=chart_yaml,
            spec=load_test_spec(path / "test-spec.yaml"),
        )

    def value_paths(self, chart: Chart, profile: str) -> list[Path]:
        """Resolve a profile's values files to absolute paths, all must exist."""
        profile_spec = chart.spec.profile(profile)
        paths = [chart.path / value for value in profile_spec.values]
        missing = [path for path in paths if not path.exists()]
        if missing:
            rendered = ", ".join(str(path) for path in missing)
            raise SpecError(f"missing values file(s) for {chart.name}:{profile}: {rendered}")
        return paths
