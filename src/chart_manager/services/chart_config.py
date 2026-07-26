"""The authored ``chart-manager.yaml`` boundary.

The envelope composes independently owned capability specifications. It is
the one place that understands the per-chart configuration file; Helm chart
discovery remains configuration-agnostic.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from chart_manager.plumbing.errors import CapabilityUnavailableError, SpecError
from chart_manager.plumbing.yaml_files import load_yaml_file
from chart_manager.services.domain.cluster_tests import ClusterTestSpec
from chart_manager.services.manifest_validation.spec import ManifestValidationSpec

CONFIG_FILENAME = "chart-manager.yaml"
LEGACY_CONFIG_FILENAMES = (
    "manifest-validation.yaml",
    "cluster-test.yaml",
    "validate-spec.yaml",
    "test-spec.yaml",
)


class CapabilityStatus(StrEnum):
    """Whether a chart-manager capability can be used."""

    ABSENT = "absent"
    DISABLED = "disabled"
    ENABLED = "enabled"


class ChartManagerConfig(BaseModel):
    """Strict authored configuration for one Helm chart."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    enabled: bool = True
    manifest_validation: ManifestValidationSpec | None = Field(
        default=None,
        alias="manifestValidation",
    )
    cluster_tests: ClusterTestSpec | None = Field(
        default=None,
        alias="clusterTests",
    )

    @model_validator(mode="after")
    def enabled_config_has_a_capability(self) -> ChartManagerConfig:
        """Reject an enabled envelope that configures no behavior."""
        if self.enabled and self.manifest_validation is None and self.cluster_tests is None:
            raise ValueError(
                "enabled chart-manager configuration must declare "
                "manifestValidation or clusterTests"
            )
        return self


def load_chart_manager_config(path: Path) -> ChartManagerConfig:
    """Strictly load one ``chart-manager.yaml`` file."""
    if not path.exists():
        _raise_for_legacy_config(path)
        raise SpecError(f"missing chart-manager configuration: {path}")
    try:
        document = load_yaml_file(path)
    except (SpecError, yaml.YAMLError) as exc:
        raise SpecError(f"invalid chart-manager configuration {path}: {exc}") from exc
    try:
        return ChartManagerConfig.model_validate(document)
    except ValueError as exc:
        raise SpecError(f"invalid chart-manager configuration {path}: {exc}") from exc


def load_optional_chart_manager_config(path: Path) -> ChartManagerConfig | None:
    """Load a config when present; malformed present files still fail."""
    if not path.exists():
        _raise_for_legacy_config(path)
        return None
    return load_chart_manager_config(path)


def _raise_for_legacy_config(path: Path) -> None:
    legacy = [name for name in LEGACY_CONFIG_FILENAMES if (path.parent / name).exists()]
    if legacy:
        names = ", ".join(legacy)
        raise SpecError(
            f"legacy chart-manager configuration found beside {path}: {names}; "
            f"migrate to {CONFIG_FILENAME}"
        )


def manifest_validation_status(
    config: ChartManagerConfig | None,
) -> CapabilityStatus:
    """Return effective manifest-validation availability."""
    if config is None or config.manifest_validation is None:
        return CapabilityStatus.ABSENT
    if not config.enabled or not config.manifest_validation.enabled:
        return CapabilityStatus.DISABLED
    return CapabilityStatus.ENABLED


def cluster_tests_status(config: ChartManagerConfig | None) -> CapabilityStatus:
    """Return effective live-cluster-test availability."""
    if config is None or config.cluster_tests is None:
        return CapabilityStatus.ABSENT
    if not config.enabled or not config.cluster_tests.enabled:
        return CapabilityStatus.DISABLED
    return CapabilityStatus.ENABLED


def require_manifest_validation(
    config: ChartManagerConfig | None,
    *,
    chart_name: str,
) -> ManifestValidationSpec:
    """Return an enabled manifest-validation section or raise precisely."""
    if config is None:
        raise CapabilityUnavailableError(
            f"chart '{chart_name}' has no manifestValidation configuration "
            f"in {CONFIG_FILENAME}"
        )
    if not config.enabled:
        raise CapabilityUnavailableError(f"chart-manager is disabled for chart '{chart_name}'")
    if config.manifest_validation is None:
        raise CapabilityUnavailableError(
            f"chart '{chart_name}' has no manifestValidation configuration "
            f"in {CONFIG_FILENAME}"
        )
    if not config.manifest_validation.enabled:
        raise CapabilityUnavailableError(
            f"manifest validation is disabled for chart '{chart_name}'"
        )
    return config.manifest_validation


def require_cluster_tests(
    config: ChartManagerConfig | None,
    *,
    chart_name: str,
) -> ClusterTestSpec:
    """Return an enabled cluster-test section or raise precisely."""
    if config is None:
        raise CapabilityUnavailableError(
            f"chart '{chart_name}' has no clusterTests configuration in {CONFIG_FILENAME}"
        )
    if not config.enabled:
        raise CapabilityUnavailableError(f"chart-manager is disabled for chart '{chart_name}'")
    if config.cluster_tests is None:
        raise CapabilityUnavailableError(
            f"chart '{chart_name}' has no clusterTests configuration in {CONFIG_FILENAME}"
        )
    if not config.cluster_tests.enabled:
        raise CapabilityUnavailableError(f"cluster tests are disabled for chart '{chart_name}'")
    return config.cluster_tests
