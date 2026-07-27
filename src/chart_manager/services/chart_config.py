"""Strict authored ``ChartLifecycle`` intent.

``chart-lifecycle.yaml`` is the only per-chart lifecycle document.  This
module owns its schema and loading boundary; catalogs compose the document
with Helm metadata and enforce identity agreement.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from chart_manager.plumbing.errors import CapabilityUnavailableError, SpecError
from chart_manager.plumbing.yaml_files import load_yaml_file
from chart_manager.services.domain.cluster_tests import ClusterTestSpec
from chart_manager.services.manifest_validation.spec import ManifestValidationSpec

LIFECYCLE_FILENAME = "chart-lifecycle.yaml"
LIFECYCLE_API_VERSION = "lifecycle.cmg.io/v1alpha1"
LIFECYCLE_KIND = "ChartLifecycle"


class CapabilityStatus(StrEnum):
    """Whether an authored lifecycle capability can be used."""

    ABSENT = "absent"
    DISABLED = "disabled"
    ENABLED = "enabled"


class ChartLifecycleMetadata(BaseModel):
    """Identity of the chart governed by a lifecycle document."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def name_must_be_exact(cls, name: str) -> str:
        """Reject whitespace-only and silently normalized chart names."""
        if name != name.strip():
            raise ValueError("metadata.name must not have leading or trailing whitespace")
        return name


class ChartLifecycleSpec(BaseModel):
    """Capabilities authored for one chart."""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool = True
    validation: ManifestValidationSpec | None = None
    cluster_test: ClusterTestSpec | None = Field(default=None, alias="clusterTest")


class ChartLifecycle(BaseModel):
    """Kubernetes-style lifecycle intent envelope for one Helm chart."""

    model_config = ConfigDict(extra="forbid", strict=True)

    api_version: Literal["lifecycle.cmg.io/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["ChartLifecycle"]
    metadata: ChartLifecycleMetadata
    spec: ChartLifecycleSpec


def load_chart_lifecycle(path: Path) -> ChartLifecycle:
    """Strictly load one ``chart-lifecycle.yaml`` document."""
    if path.name != LIFECYCLE_FILENAME:
        raise SpecError(
            f"lifecycle configuration must be named {LIFECYCLE_FILENAME}: {path}"
        )
    if not path.exists():
        raise SpecError(f"missing chart lifecycle configuration: {path}")
    try:
        document = load_yaml_file(path)
    except (SpecError, yaml.YAMLError) as exc:
        raise SpecError(f"invalid chart lifecycle configuration {path}: {exc}") from exc
    try:
        return ChartLifecycle.model_validate(document)
    except ValueError as exc:
        raise SpecError(f"invalid chart lifecycle configuration {path}: {exc}") from exc


def load_optional_chart_lifecycle(path: Path) -> ChartLifecycle | None:
    """Load lifecycle intent when present; malformed present files still fail."""
    if path.name != LIFECYCLE_FILENAME:
        raise SpecError(
            f"lifecycle configuration must be named {LIFECYCLE_FILENAME}: {path}"
        )
    if not path.exists():
        return None
    return load_chart_lifecycle(path)


def validate_chart_lifecycle_identity(
    lifecycle: ChartLifecycle,
    *,
    chart_name: str,
    chart_directory: Path,
) -> None:
    """Require lifecycle, Helm, and directory identities to agree.

    ``ChartRepository.get`` has already established that ``chart_name`` is
    both the requested directory name and ``Chart.yaml`` name.  Keeping this
    final comparison at composition sites avoids coupling the standalone
    lifecycle schema to Helm discovery.
    """
    directory_name = chart_directory.name
    if lifecycle.metadata.name != chart_name or directory_name != chart_name:
        raise SpecError(
            f"{chart_directory / LIFECYCLE_FILENAME} metadata.name "
            f"'{lifecycle.metadata.name}' does not match chart directory "
            f"'{directory_name}' and Chart.yaml name '{chart_name}'"
        )


def validation_status(lifecycle: ChartLifecycle | None) -> CapabilityStatus:
    """Return effective manifest-validation availability."""
    if lifecycle is None or lifecycle.spec.validation is None:
        return CapabilityStatus.ABSENT
    if not lifecycle.spec.enabled or not lifecycle.spec.validation.enabled:
        return CapabilityStatus.DISABLED
    return CapabilityStatus.ENABLED


def cluster_test_status(lifecycle: ChartLifecycle | None) -> CapabilityStatus:
    """Return effective live-cluster-test availability."""
    if lifecycle is None or lifecycle.spec.cluster_test is None:
        return CapabilityStatus.ABSENT
    if not lifecycle.spec.enabled or not lifecycle.spec.cluster_test.enabled:
        return CapabilityStatus.DISABLED
    return CapabilityStatus.ENABLED


def require_validation(
    lifecycle: ChartLifecycle | None,
    *,
    chart_name: str,
) -> ManifestValidationSpec:
    """Return an enabled validation section or raise precisely."""
    if lifecycle is None or lifecycle.spec.validation is None:
        raise CapabilityUnavailableError(
            f"chart '{chart_name}' has no validation configuration in "
            f"{LIFECYCLE_FILENAME}"
        )
    if not lifecycle.spec.enabled:
        raise CapabilityUnavailableError(f"ChartLifecycle is disabled for chart '{chart_name}'")
    if not lifecycle.spec.validation.enabled:
        raise CapabilityUnavailableError(
            f"manifest validation is disabled for chart '{chart_name}'"
        )
    return lifecycle.spec.validation


def require_cluster_test(
    lifecycle: ChartLifecycle | None,
    *,
    chart_name: str,
) -> ClusterTestSpec:
    """Return an enabled cluster-test section or raise precisely."""
    if lifecycle is None or lifecycle.spec.cluster_test is None:
        raise CapabilityUnavailableError(
            f"chart '{chart_name}' has no clusterTest configuration in "
            f"{LIFECYCLE_FILENAME}"
        )
    if not lifecycle.spec.enabled:
        raise CapabilityUnavailableError(f"ChartLifecycle is disabled for chart '{chart_name}'")
    if not lifecycle.spec.cluster_test.enabled:
        raise CapabilityUnavailableError(f"cluster tests are disabled for chart '{chart_name}'")
    return lifecycle.spec.cluster_test
