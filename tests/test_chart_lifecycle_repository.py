"""Repository-wide guards for the authored ChartLifecycle contract."""

from __future__ import annotations

import yaml

from chart_manager.api.lifecycle.v1alpha1 import LIFECYCLE_API_VERSION, LIFECYCLE_KIND
from chart_manager.domain.lifecycle_policy import (
    LIFECYCLE_FILENAME,
    CapabilityStatus,
    cluster_test_status,
    load_chart_lifecycle,
    validation_status,
)

from .conftest import REPO_ROOT


def test_every_production_chart_has_one_valid_enabled_config() -> None:
    chart_dirs = sorted(path.parent for path in (REPO_ROOT / "charts").glob("*/Chart.yaml"))

    assert len(chart_dirs) == 28
    for chart_dir in chart_dirs:
        config_path = chart_dir / LIFECYCLE_FILENAME
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert list(document) == ["apiVersion", "kind", "metadata", "spec"], chart_dir.name
        assert document["apiVersion"] == LIFECYCLE_API_VERSION, chart_dir.name
        assert document["kind"] == LIFECYCLE_KIND, chart_dir.name
        assert document["metadata"] == {"name": chart_dir.name}, chart_dir.name
        assert list(document["spec"]) == [
            "enabled",
            "validation",
            "clusterTest",
        ], chart_dir.name

        lifecycle = load_chart_lifecycle(config_path)
        assert lifecycle.spec.enabled, chart_dir.name
        assert validation_status(lifecycle) is CapabilityStatus.ENABLED, chart_dir.name
        assert cluster_test_status(lifecycle) is CapabilityStatus.ENABLED, chart_dir.name

def test_no_helmignore_excludes_chart_lifecycle_configuration() -> None:
    offenders = []
    for ignore_path in (REPO_ROOT / "charts").glob("*/.helmignore"):
        entries = {
            line.strip()
            for line in ignore_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        if LIFECYCLE_FILENAME in entries:
            offenders.append(ignore_path.relative_to(REPO_ROOT))

    assert offenders == []
