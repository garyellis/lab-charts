"""HelmRelease promotion across Flux GitOps repos.

`HelmReleaseRef` is re-exported deliberately. It is a frozen identity record
with no behavior, it already appears on this package's public result types
(`MonitorOutcome.ref`, `TestOutcome.ref`, `NO_MATCH_REF`) and in the progress-
callback signature, and it is defined in `integrations/helmrelease.py` only
because that is where it was first parsed. Surfaces that need to *name* the type --
e.g. `cli/helmrelease_render.py` typing its progress driver -- import it from
here, so no surface has to reach into `integrations/` for a type annotation.

The underlying question (that `plumbing/` and `integrations/` both hold domain
model which probably wants its own `domain/` package) is tracked separately;
this re-export is the local fix, not that split.
"""

from chart_manager.integrations.helmrelease import HelmReleaseRef

from .editor import EditResult, set_version
from .monitor import MonitorOutcome, MonitorRequest, MonitorResult, MonitorService
from .promote import PromoteRequest, PromoteResult, PromoteService
from .scanner import HelmReleaseMatch, scan
from .state import (
    NO_MATCH_REF,
    PASSING_VERDICTS,
    PromoteStatus,
    Reason,
    Transition,
    Verdict,
)
from .test import TestOutcome, TestPodSnapshot, TestRequest, TestResult, TestService
from .wire import SCHEMA_VERSION, monitor_to_dict, test_to_dict

__all__ = [
    "NO_MATCH_REF",
    "PASSING_VERDICTS",
    "SCHEMA_VERSION",
    "EditResult",
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
    "Reason",
    "TestOutcome",
    "TestPodSnapshot",
    "TestRequest",
    "TestResult",
    "TestService",
    "Transition",
    "Verdict",
    "monitor_to_dict",
    "scan",
    "set_version",
    "test_to_dict",
]
