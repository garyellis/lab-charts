"""`promote pr|monitor|test`: open the Promotion PR, watch the rollout, run the promotion test.

The package exports the request and outcome types; each stage's `run()` lives in its module.
"""

from .monitor import MonitorOutcome, MonitorRequest, MonitorResult
from .pr import PromoteRequest, PromoteResult
from .scanner import HelmReleaseMatch
from .state import PromoteStatus, Transition
from .test import TestOutcome, TestRequest, TestResult

__all__ = [
    "HelmReleaseMatch",
    "MonitorOutcome",
    "MonitorRequest",
    "MonitorResult",
    "PromoteRequest",
    "PromoteResult",
    "PromoteStatus",
    "TestOutcome",
    "TestRequest",
    "TestResult",
    "Transition",
]
