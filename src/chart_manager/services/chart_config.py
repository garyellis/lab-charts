"""Loading and capability policy for authored ``ChartLifecycle`` intent.

``chart-lifecycle.yaml`` is the only per-chart lifecycle document.  Its
accepted shape is owned by ``chart_manager.api.lifecycle.v1alpha1``; this
module owns the boundary around it -- where the file lives, how a decode
failure becomes a ``SpecError``, and whether an authored capability is
usable.  Catalogs compose the document with Helm metadata and enforce
identity agreement.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml

from chart_manager.api.lifecycle.v1alpha1 import (
    ChartLifecycle,
    ClusterTestSpec,
    ManifestValidationSpec,
)
from chart_manager.plumbing.errors import CapabilityUnavailableError, SpecError
from chart_manager.plumbing.yaml_files import load_yaml_file

LIFECYCLE_FILENAME = "chart-lifecycle.yaml"


class CapabilityStatus(StrEnum):
    """Whether an authored lifecycle capability can be used."""

    ABSENT = "absent"
    DISABLED = "disabled"
    ENABLED = "enabled"


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
