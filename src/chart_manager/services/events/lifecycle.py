"""The PlatformLifecycleEvent schema and its build/promotion phase enums."""
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4


class BuildPhase(str, Enum):
    """Phases of the charts-repo build lifecycle (PR open through published)."""

    PR_OPEN         = "pr_open"
    VALIDATING      = "validating"
    VALIDATION_OK   = "validation_ok"
    VALIDATION_FAIL = "validation_fail"
    MERGED          = "merged"
    PREVIEW_PUBLISHED = "preview_published"
    PUBLISHED       = "published"

class PromotionPhase(str, Enum):
    """Phases of the flux-repo promotion lifecycle (detected through reached-prod)."""

    DETECTED         = "detected"
    FLUX_PR_OPEN     = "flux_pr_open"
    AWAITING_MERGE   = "awaiting_merge"
    WAITING_ROLLOUT  = "waiting_for_rollout"
    ROLLOUT_OK       = "rollout_complete"
    HELM_TEST_RUN    = "helm_test_running"
    HELM_TEST_OK     = "helm_test_passed"
    HELM_TEST_FAILED = "helm_test_failed"
    PROMOTED         = "promoted"
    REACHED_PROD     = "reached_prod"
    ABANDONED        = "abandoned"

@dataclass(frozen=True,kw_only=True)
class PlatformLifecycleEvent:
    """One immutable lifecycle event; exactly one of build_phase/promotion_phase is set."""

    # identity
    uuid: UUID = field(default_factory=uuid4)
    correlation_id: str | None     # f"{chart_name}@{chart_version}"; the join
                                   # key, NOT the partition key (see store.py)
    build_correlation_id: str | None     # the charts repo PR (build lifecycle)
    promotion_correlation_id: str | None # the flux repo PR (promotion lifecycle)

    # unit
    chart_name: str           # the store partition key: chart-scoped, so one
                              # chart's whole history is a single-partition read

    chart_version: str | None # None while PR is open and version not published
    images: tuple[str, ...]
    environment: str | None   # None for the build lifecycle; set in Flux

    # transition - exactly one of these is set
    build_phase: BuildPhase | None
    promotion_phase: PromotionPhase | None

    timestamp: datetime
    source: str
    pr_url: str | None
    git_sha: str | None
    detail: dict[str, Any] | None
    # Stable identity for retry-safe transitions. Legacy/ad-hoc events leave
    # this unset and retain append-only UUID identity.
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict with uuid and timestamp as strings (store-ready)."""
        d = asdict(self)
        d["uuid"] = str(self.uuid)
        d["timestamp"] = self.timestamp.isoformat()
        if self.idempotency_key is None:
            d.pop("idempotency_key")
        return d
