"""Repository-wide guards for the authored chart-manager contract."""

from __future__ import annotations

from chart_manager.services.chart_config import (
    CONFIG_FILENAME,
    LEGACY_CONFIG_FILENAMES,
    CapabilityStatus,
    cluster_tests_status,
    load_chart_manager_config,
    manifest_validation_status,
)

from .conftest import REPO_ROOT


def test_every_production_chart_has_one_valid_enabled_config() -> None:
    chart_dirs = sorted(path.parent for path in (REPO_ROOT / "charts").glob("*/Chart.yaml"))

    assert len(chart_dirs) == 28
    for chart_dir in chart_dirs:
        config = load_chart_manager_config(chart_dir / CONFIG_FILENAME)
        assert config.enabled, chart_dir.name
        assert manifest_validation_status(config) is CapabilityStatus.ENABLED, chart_dir.name
        assert cluster_tests_status(config) is CapabilityStatus.ENABLED, chart_dir.name


def test_repository_contains_no_legacy_chart_configuration_files() -> None:
    roots = (REPO_ROOT / "charts", REPO_ROOT / "tests" / "fixtures" / "charts")

    found = [
        path.relative_to(REPO_ROOT)
        for root in roots
        for legacy_name in LEGACY_CONFIG_FILENAMES
        for path in root.glob(f"*/{legacy_name}")
    ]

    assert found == []


def test_no_helmignore_excludes_chart_manager_configuration() -> None:
    offenders = []
    for ignore_path in (REPO_ROOT / "charts").glob("*/.helmignore"):
        entries = {
            line.strip()
            for line in ignore_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        if CONFIG_FILENAME in entries:
            offenders.append(ignore_path.relative_to(REPO_ROOT))

    assert offenders == []
