"""Prove ChartLifecycle intent remains in Helm package artifacts."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from chart_manager.services.chart_config import LIFECYCLE_FILENAME

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).parents[2]
def test_every_production_chart_package_contains_chart_lifecycle(
    tmp_path: Path,
) -> None:
    if shutil.which("helm") is None:
        pytest.skip("missing tool on PATH: helm")

    chart_dirs = sorted(path.parent for path in (REPO_ROOT / "charts").glob("*/Chart.yaml"))
    assert len(chart_dirs) == 28

    for chart_dir in chart_dirs:
        subprocess.run(
            ["helm", "package", str(chart_dir), "--destination", str(tmp_path)],
            check=True,
            capture_output=True,
            text=True,
        )

    archives = sorted(tmp_path.glob("*.tgz"))
    assert len(archives) == len(chart_dirs)
    for archive in archives:
        with tarfile.open(archive, mode="r:gz") as package:
            members = {member.name for member in package.getmembers()}
        packaged_configs = {
            member for member in members if member.endswith(f"/{LIFECYCLE_FILENAME}")
        }
        assert len(packaged_configs) == 1, archive.name
