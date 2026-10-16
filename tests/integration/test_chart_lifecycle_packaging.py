"""Prove ChartLifecycle intent remains in Helm package artifacts."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from chart_manager.api.v1alpha1.common import API_VERSION
from chart_manager.plumbing.yaml_files import dump_yaml, parse_yaml
from chart_manager.shared.charts.lifecycle import LIFECYCLE_FILENAME
from tests.integration.conftest import require

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).parents[2]


def _stage_without_dependencies(chart_dir: Path, staging_root: Path) -> Path:
    """Copy a chart to staging with its `dependencies` stanza removed.

    `helm package` refuses to run when a declared dependency is not vendored
    under charts/, and vendored subchart archives are gitignored — so on a
    clean checkout (CI) every chart with dependencies would fail to package.
    Building them would put a network fetch of 25 upstream charts in the fast
    gate. This contract is about the chart's own files surviving packaging
    (.helmignore rules, the lifecycle filename), which subcharts do not affect,
    so we package the chart standalone instead.
    """
    staged = staging_root / chart_dir.name
    shutil.copytree(chart_dir, staged)
    shutil.rmtree(staged / "charts", ignore_errors=True)

    metadata_path = staged / "Chart.yaml"
    metadata = parse_yaml(metadata_path.read_text())
    if metadata.pop("dependencies", None) is not None:
        metadata_path.write_text(dump_yaml(metadata))
    return staged


def test_every_production_chart_package_contains_chart_lifecycle(
    tmp_path: Path,
) -> None:
    require("helm")

    chart_dirs = sorted(path.parent for path in (REPO_ROOT / "charts").glob("*/Chart.yaml"))
    assert chart_dirs

    staging_root = tmp_path / "src"
    staging_root.mkdir()
    packages = tmp_path / "packages"
    packages.mkdir()

    for chart_dir in chart_dirs:
        staged = _stage_without_dependencies(chart_dir, staging_root)
        subprocess.run(
            ["helm", "package", str(staged), "--destination", str(packages)],
            check=True,
            capture_output=True,
            text=True,
        )

    archives = sorted(packages.glob("*.tgz"))
    assert len(archives) == len(chart_dirs)
    for archive in archives:
        with tarfile.open(archive, mode="r:gz") as package:
            packaged_configs = [
                member
                for member in package.getmembers()
                if member.name.endswith(f"/{LIFECYCLE_FILENAME}")
            ]
            assert len(packaged_configs) == 1, archive.name
            config = package.extractfile(packaged_configs[0])
            assert config is not None
            document = parse_yaml(config.read(), source=packaged_configs[0].name)
        assert document["apiVersion"] == API_VERSION, archive.name
