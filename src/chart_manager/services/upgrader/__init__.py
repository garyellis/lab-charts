"""Renovate-driven wrapper chart upgrade service."""

from chart_manager.services.upgrader.errors import UpgradeError
from chart_manager.services.upgrader.finalizer import (
    BaselineReader,
    GitBaselineReader,
    UpgradeFinalizer,
    load_update_data,
)
from chart_manager.services.upgrader.models import (
    FinalizeRequest,
    FinalizeResult,
    UpdateMetadata,
    UpgradePlan,
    UpgradeRequest,
    UpgradeResult,
)
from chart_manager.services.upgrader.paths import resolve_chart_path
from chart_manager.services.upgrader.service import (
    PullRequestLike,
    PullRequestLookup,
    RelevantChanges,
    RenovateAdapter,
    RenovateRequestFactory,
    UpgradeService,
    build_upgrade_plan,
)
from chart_manager.services.upgrader.telemetry import OUTCOME_PHASE, UpgradeTelemetry

__all__ = [
    "OUTCOME_PHASE",
    "BaselineReader",
    "FinalizeRequest",
    "FinalizeResult",
    "GitBaselineReader",
    "PullRequestLike",
    "PullRequestLookup",
    "RelevantChanges",
    "RenovateAdapter",
    "RenovateRequestFactory",
    "UpdateMetadata",
    "UpgradeError",
    "UpgradeFinalizer",
    "UpgradePlan",
    "UpgradeRequest",
    "UpgradeResult",
    "UpgradeService",
    "UpgradeTelemetry",
    "build_upgrade_plan",
    "load_update_data",
    "resolve_chart_path",
]
