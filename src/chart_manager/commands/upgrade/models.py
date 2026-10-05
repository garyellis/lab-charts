"""Public, serializable vocabulary for chart upgrades and their finalizer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from chart_manager.plumbing.errors import ChartManagerError


class UpgradeError(ChartManagerError):
    """An upgrade request is unsafe or inconsistent with repository state."""


@dataclass(frozen=True)
class UpgradeRequest:
    """Ask Renovate to discover and apply updates for one wrapper chart."""

    root: Path
    chart_path: Path
    dry_run: bool = False


class UpgradeStatus(StrEnum):
    """What one `chart upgrade` run left on GitHub."""

    DRY_RUN = "dry_run"
    NO_CHANGES = "no_changes"
    PR_OPEN = "pr_open"
    PR_UPDATED = "pr_updated"
    STATUS_UNKNOWN = "status_unknown"


@dataclass(frozen=True)
class UpgradeResult:
    """Stable service outcome; adapter-specific output stays diagnostic-only."""

    chart: str
    chart_path: Path
    current_version: str
    proposed_version: str | None
    branch: str | None
    group: str
    outcome: UpgradeStatus
    diagnostics: tuple[str, ...] = ()
    repository: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None

    @property
    def changed(self) -> bool:
        """Whether an update proposal was produced."""
        return self.proposed_version is not None


@dataclass(frozen=True)
class UpdateMetadata:
    """The small, trusted subset of Renovate update metadata we consume."""

    dependency: str
    current_version: str
    new_version: str
    manager: str = ""
    datasource: str = ""
    update_type: str = ""
    # Repo-relative file the dependency was found in, e.g.
    # "charts/grafana/values.yaml". Excluded from equality so that one
    # dependency pinned across several values files still collapses to a single
    # changelog line, exactly as it did before this field existed. A future
    # multi-chart run must therefore filter by this field *before* de-duplicating,
    # or one chart's entry would absorb another's.
    package_file: str = field(default="", compare=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> UpdateMetadata:
        """Normalize Renovate's camelCase result fields."""

        def text(*names: str) -> str:
            for name in names:
                item = value.get(name)
                if item is not None:
                    return str(item)
            return ""

        return cls(
            dependency=text("dependency", "depName", "packageName"),
            current_version=text("current_version", "currentVersion", "currentValue"),
            new_version=text("new_version", "newVersion", "newValue"),
            manager=text("manager"),
            datasource=text("datasource"),
            update_type=text("update_type", "updateType"),
            package_file=text("package_file", "packageFile"),
        )

    @property
    def is_chart_dependency(self) -> bool:
        """True for a Helm Chart.yaml dependency update."""
        return (
            self.manager.lower() in {"helmv3", "helm-requirements"}
            or self.datasource.lower() == "helm"
        )

    @property
    def is_container_image(self) -> bool:
        """True for a container image update."""
        return self.datasource.lower() in {"docker", "dockerfile"} or self.manager.lower() in {
            "dockerfile",
            "docker-compose",
            "kubernetes",
        }

    @property
    def qualifies(self) -> bool:
        """Only image and Helm dependency changes affect wrapper versions."""
        return self.is_chart_dependency or self.is_container_image


@dataclass(frozen=True)
class FinalizeRequest:
    """Finalize Renovate's edits to one chart against its Chart.yaml at HEAD."""

    repo_root: Path
    chart_path: Path
    update_data: Mapping[str, Any]


@dataclass(frozen=True)
class FinalizeResult:
    """Files and version selected by one deterministic finalizer pass."""

    chart: str
    previous_version: str
    version: str
    bump: str | None
    changed: bool
    files: tuple[Path, ...] = ()
    updates: tuple[UpdateMetadata, ...] = ()


@dataclass(frozen=True)
class UpgradePlan:
    """Validated chart identity and deterministic Renovate inputs."""

    repo_root: Path
    chart_path: Path
    chart: str
    current_version: str
    branch_prefix: str
    group: str
    runtime_overlay: Mapping[str, object] = field(default_factory=dict)
