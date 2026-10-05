"""HelmRelease promotion across Flux GitOps repos.

`HelmReleaseRef` is re-exported deliberately. It is a frozen identity record
with no behavior, it already appears on this package's public result types
(`MonitorOutcome.ref`, `TestOutcome.ref`, `NO_MATCH_REF`) and in the progress-
callback signature, and it is defined in `integrations/helmrelease.py` only
because that is where it was first parsed. Surfaces that need to *name* the type --
e.g. `commands/promote/render.py` typing its progress driver -- import it from
here, so no surface has to reach into `integrations/` for a type annotation.

The underlying question -- whether the Flux identity records `plumbing/` and
`integrations/` hold belong in `chart_manager.domain` alongside the chart
policy -- is still open, and deliberately so: `domain/` today is chart and
lifecycle policy, and moving a Flux type in would need the same "what is it?"
answer, not just a free slot. This re-export is the local fix, not that split.

Scope boundary (kept from this package's deleted README, which was otherwise
stale planning prose): one `PromoteService.promote()` call promotes one chart
into one (path, environment). Deciding *which* environments a publish fans out
to belongs to the orchestrator that triggers it -- multi-env fan-out is N
separate calls -- and so does gating on cluster state (`Ready=True`,
`TestSuccess=True`), which the trigger waits for before firing the next call.
Any future non-CLI surface reuses `PromoteService.promote()` unchanged rather
than re-deciding either question.
"""

from chart_manager.integrations.helmrelease import HelmReleaseRef

from .monitor import MonitorOutcome, MonitorRequest, MonitorResult, MonitorService
from .pr import PromoteRequest, PromoteResult, PromoteService
from .scanner import HelmReleaseMatch
from .state import (
    NO_MATCH_REF,
    PASSING_VERDICTS,
    PROMOTE_OUTCOME,
    PromoteStatus,
    Transition,
)
from .test import TestOutcome, TestRequest, TestResult, TestService
from .wire import (
    monitor_to_dict,
    promote_to_dict,
    test_to_dict,
)

__all__ = [
    "NO_MATCH_REF",
    "PASSING_VERDICTS",
    "PROMOTE_OUTCOME",
    "HelmReleaseMatch",
    "HelmReleaseRef",
    "MonitorOutcome",
    "MonitorRequest",
    "MonitorResult",
    "MonitorService",
    "PromoteRequest",
    "PromoteResult",
    "PromoteService",
    "PromoteStatus",
    "TestOutcome",
    "TestRequest",
    "TestResult",
    "TestService",
    "Transition",
    "monitor_to_dict",
    "promote_to_dict",
    "test_to_dict",
]
