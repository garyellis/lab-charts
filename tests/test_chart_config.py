"""Tests for the unified per-chart configuration boundary."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from chart_manager.plumbing.errors import CapabilityUnavailableError, SpecError
from chart_manager.services.chart_config import (
    CapabilityStatus,
    ChartManagerConfig,
    cluster_tests_status,
    load_chart_manager_config,
    load_optional_chart_manager_config,
    manifest_validation_status,
    require_cluster_tests,
    require_manifest_validation,
)


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "chart-manager.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _cluster_config(*, root_enabled: bool = True, section_enabled: bool = True) -> str:
    return f"""
version: 1
enabled: {str(root_enabled).lower()}
clusterTests:
  enabled: {str(section_enabled).lower()}
  profiles:
    minimal:
      values: [values.yaml]
      helmTest: true
  dependentTests: []
"""


def _manifest_config(*, root_enabled: bool = True, section_enabled: bool = True) -> str:
    return f"""
version: 1
enabled: {str(root_enabled).lower()}
manifestValidation:
  enabled: {str(section_enabled).lower()}
  releaseName: demo
  namespaceTemplate: "lab-${{env}}"
  environments:
    ci:
      values: [values.yaml]
  triggers:
    templates/**: [ci]
  unmatchedChanges: warn
"""


def test_loads_each_capability_with_authored_camel_case(tmp_path: Path) -> None:
    cluster = load_chart_manager_config(_write_config(tmp_path, _cluster_config()))

    assert cluster.version == 1
    assert cluster.cluster_tests is not None
    assert cluster.cluster_tests.profile("minimal").helm_test is True
    assert cluster_tests_status(cluster) is CapabilityStatus.ENABLED

    manifest = ChartManagerConfig.model_validate(
        {
            "version": 1,
            "manifestValidation": {
                "releaseName": "demo",
                "namespaceTemplate": "lab-${env}",
                "helmVersion": "3.20.0",
                "schemaLocations": ["default"],
                "environments": {"ci": {"values": ["values.yaml"]}},
                "triggerIgnores": ["README.md"],
                "unmatchedChanges": "all-environments",
            },
        }
    )
    assert manifest.manifest_validation is not None
    assert manifest.manifest_validation.release_name == "demo"
    assert manifest.manifest_validation.unmatched_changes == "all-environments"


def test_both_capabilities_can_share_one_envelope(tmp_path: Path) -> None:
    config = load_chart_manager_config(
        _write_config(
            tmp_path,
            _manifest_config()
            + """
clusterTests:
  profiles:
    minimal: {}
""",
        )
    )

    assert manifest_validation_status(config) is CapabilityStatus.ENABLED
    assert cluster_tests_status(config) is CapabilityStatus.ENABLED


def test_root_disabled_may_omit_all_capabilities(tmp_path: Path) -> None:
    config = load_chart_manager_config(
        _write_config(tmp_path, "version: 1\nenabled: false\n")
    )

    assert config.enabled is False
    assert manifest_validation_status(config) is CapabilityStatus.ABSENT
    assert cluster_tests_status(config) is CapabilityStatus.ABSENT


def test_enabled_root_requires_a_capability(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="must declare manifestValidation or clusterTests"):
        load_chart_manager_config(_write_config(tmp_path, "version: 1\nenabled: true\n"))


@pytest.mark.parametrize(
    "body",
    [
        "enabled: false\n",
        "version: 2\nenabled: false\n",
        "version: 1\nenabled: false\nmystery: true\n",
        "version: 1\nenabled: false\ncluster_tests: null\n",
        "version: 1\nclusterTests:\n  version: 1\n  profiles:\n    minimal: {}\n",
        "version: 1\nclusterTests:\n  profiles: {}\n  dependent_tests: []\n",
        (
            "version: 1\nmanifestValidation:\n"
            "  release_name: demo\n"
            "  environments:\n"
            "    ci:\n"
            "      namespace: demo\n"
        ),
        "- version\n- 1\n",
    ],
)
def test_root_contract_is_strict(tmp_path: Path, body: str) -> None:
    with pytest.raises(SpecError, match="invalid chart-manager configuration"):
        load_chart_manager_config(_write_config(tmp_path, body))


def test_disabled_sections_remain_schema_validated(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match=r"releaseName"):
        load_chart_manager_config(
            _write_config(
                tmp_path,
                """
version: 1
manifestValidation:
  enabled: false
  environments:
    ci:
      namespace: demo
""",
            )
        )


def test_optional_loader_only_tolerates_an_absent_file(tmp_path: Path) -> None:
    path = tmp_path / "chart-manager.yaml"
    assert load_optional_chart_manager_config(path) is None

    _write_config(tmp_path, "not: [valid")
    with pytest.raises(SpecError, match="invalid chart-manager configuration"):
        load_optional_chart_manager_config(path)


@pytest.mark.parametrize(
    "legacy_name",
    [
        "manifest-validation.yaml",
        "cluster-test.yaml",
        "validate-spec.yaml",
        "test-spec.yaml",
    ],
)
def test_absent_unified_config_rejects_legacy_files(
    tmp_path: Path,
    legacy_name: str,
) -> None:
    (tmp_path / legacy_name).write_text("version: 1\n", encoding="utf-8")

    with pytest.raises(
        SpecError,
        match=rf"legacy chart-manager configuration.*{legacy_name}.*chart-manager.yaml",
    ):
        load_optional_chart_manager_config(tmp_path / "chart-manager.yaml")


def test_strict_loader_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="missing chart-manager configuration"):
        load_chart_manager_config(tmp_path / "chart-manager.yaml")


def test_capability_status_distinguishes_absent_disabled_and_enabled() -> None:
    absent = ChartManagerConfig(version=1, enabled=False)
    disabled = ChartManagerConfig.model_validate(
        {
            "version": 1,
            "clusterTests": {"enabled": False, "profiles": {"minimal": {}}},
        }
    )
    enabled = ChartManagerConfig.model_validate(
        {"version": 1, "clusterTests": {"profiles": {"minimal": {}}}}
    )

    assert cluster_tests_status(None) is CapabilityStatus.ABSENT
    assert cluster_tests_status(absent) is CapabilityStatus.ABSENT
    assert cluster_tests_status(disabled) is CapabilityStatus.DISABLED
    assert cluster_tests_status(enabled) is CapabilityStatus.ENABLED


@pytest.mark.parametrize(
    ("config", "helper", "message"),
    [
        (
            None,
            require_manifest_validation,
            "chart 'demo' has no manifestValidation configuration in chart-manager.yaml",
        ),
        (
            ChartManagerConfig(version=1, enabled=False),
            require_manifest_validation,
            "chart-manager is disabled for chart 'demo'",
        ),
        (
            ChartManagerConfig.model_validate(
                {"version": 1, "clusterTests": {"profiles": {"minimal": {}}}}
            ),
            require_manifest_validation,
            "chart 'demo' has no manifestValidation configuration in chart-manager.yaml",
        ),
        (
            ChartManagerConfig.model_validate(
                {
                    "version": 1,
                    "manifestValidation": {
                        "enabled": False,
                        "releaseName": "demo",
                        "environments": {"ci": {"namespace": "demo"}},
                    },
                }
            ),
            require_manifest_validation,
            "manifest validation is disabled for chart 'demo'",
        ),
        (
            None,
            require_cluster_tests,
            "chart 'demo' has no clusterTests configuration in chart-manager.yaml",
        ),
        (
            ChartManagerConfig.model_validate(
                {
                    "version": 1,
                    "clusterTests": {
                        "enabled": False,
                        "profiles": {"minimal": {}},
                    },
                }
            ),
            require_cluster_tests,
            "cluster tests are disabled for chart 'demo'",
        ),
    ],
)
def test_require_helpers_report_precise_unavailable_reason(
    config: ChartManagerConfig | None,
    helper: Callable[..., object],
    message: str,
) -> None:
    with pytest.raises(CapabilityUnavailableError) as caught:
        helper(config, chart_name="demo")
    assert str(caught.value) == message
    assert not isinstance(caught.value, SpecError)
