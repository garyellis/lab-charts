"""Requests, results and errors for `chart upgrade` and `upgrade-finalize`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.shared.charts.chart import Chart


class UpgradeError(ChartManagerError):
    """An upgrade request is unsafe or inconsistent with repository state."""


@dataclass(frozen=True)
class UpgradeRequest:
    """Ask Renovate to discover and apply updates for one wrapper chart."""

    chart: Chart
    dry_run: bool = False


class UpgradeStatus(StrEnum):
    """What one `chart upgrade` run left on GitHub."""

    DRY_RUN = "dry_run"
    NO_CHANGES = "no_changes"
    PR_OPEN = "pr_open"
    PR_UPDATED = "pr_updated"
    STATUS_UNKNOWN = "status_unknown"


#: The exit outcome of each `UpgradeStatus`; an unknown pull request status is a `TOOL` failure.
UPGRADE_OUTCOME: Mapping[UpgradeStatus, Outcome] = {
    UpgradeStatus.DRY_RUN: Outcome.SUCCESS,
    UpgradeStatus.NO_CHANGES: Outcome.SUCCESS,
    UpgradeStatus.PR_OPEN: Outcome.SUCCESS,
    UpgradeStatus.PR_UPDATED: Outcome.SUCCESS,
    UpgradeStatus.STATUS_UNKNOWN: Outcome.TOOL,
}


@dataclass(frozen=True)
class UpgradeResult:
    """What one `chart upgrade` run proposed; Renovate's own output stays in diagnostics."""

    chart: str
    chart_path: Path
    current_version: str
    proposed_version: str | None
    branch: str | None
    group: str
    status: UpgradeStatus
    diagnostics: tuple[str, ...] = ()
    repository: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None

    @property
    def outcome(self) -> Outcome:
        """How the run ended, for its exit code."""
        return UPGRADE_OUTCOME[self.status]


@dataclass(frozen=True)
class UpdateMetadata:
    """The small, trusted subset of Renovate update metadata we consume."""

    dependency: str
    current_version: str
    new_version: str
    manager: str = ""
    datasource: str = ""
    update_type: str = ""

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

    chart: Chart
    update_data: Mapping[str, Any]


@dataclass(frozen=True)
class FinalizeResult:
    """The wrapper version finalize chose, and whether it changed any file."""

    chart: str
    previous_version: str
    version: str
    changed: bool

    @property
    def outcome(self) -> Outcome:
        """Finalize either succeeds or raises."""
        return Outcome.SUCCESS


@dataclass(frozen=True)
class UpgradePlan:
    """Validated chart identity and deterministic Renovate inputs."""

    repo_root: Path
    chart_path: Path
    chart: str
    current_version: str
    branch_prefix: str
    group: str
    runtime_overlay: Mapping[str, object]
