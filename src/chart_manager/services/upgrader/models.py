"""Public, serializable vocabulary for chart upgrades and their finalizer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class UpgradeRequest:
    """Ask Renovate to discover and apply updates for one wrapper chart."""

    root: Path
    chart_path: Path
    dry_run: bool = False


@dataclass(frozen=True)
class UpgradeResult:
    """Stable service outcome; adapter-specific output stays diagnostic-only."""

    chart: str
    chart_path: Path
    current_version: str
    proposed_version: str | None
    branch: str | None
    group: str
    outcome: str
    diagnostics: tuple[str, ...] = ()
    repository: str | None = None
    base: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None

    @property
    def changed(self) -> bool:
        """Whether an update proposal was produced."""
        return self.proposed_version is not None

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON-facing representation used by surfaces."""
        return {
            "repository": self.repository,
            "base": self.base,
            "chart": self.chart,
            "chart_path": str(self.chart_path),
            "current_version": self.current_version,
            "proposed_version": self.proposed_version,
            "branch": self.branch,
            "group": self.group,
            "outcome": self.outcome,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True)
class UpdateMetadata:
    """The small, trusted subset of Renovate update metadata we consume."""

    dependency: str
    current_version: str
    new_version: str
    manager: str = ""
    datasource: str = ""
    update_type: str = ""

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
    """Finalize Renovate-authored files relative to a known baseline."""

    repo_root: Path
    chart_path: Path
    updates: tuple[UpdateMetadata, ...] = ()
    update_data: Mapping[str, Any] | None = None
    baseline_ref: str = "HEAD"
    target_heading: str | None = None
    dry_run: bool = False


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
