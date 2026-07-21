from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

class BuildPhase(str, Enum):
    PR_OPEN         = "pr_open"
    VALIDATING      = "validating"
    VALIDATION_OK   = "validation_ok"
    VALIDATION_FAIL = "validation_fail"
    MERGED          = "merged"
    PUBLISHED       = "published"

class PromotionPhase(str, Enum):
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
    # identity
    uuid: UUID = field(default_factory=uuid4)
    correlation_id: str | None     # f"{chart_name}@{chart_version}"
    build_correlation_id: str | None     # the charts repo PR (build lifecycle)
    promotion_correlation_id: str | None # the flux repo PR (promotion lifecycle)

    # unit
    chart_name: str
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
    detail: dict | None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["uuid"] = str(self.uuid)
        d["timestamp"] = self.timestamp.isoformat()
        return d


