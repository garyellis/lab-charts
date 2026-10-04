"""`chart test` and `chart teardown`: requests, the compiled plan, and what a run did."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from chart_manager.api.v1alpha1.chart_lifecycle import DEFAULT_PROFILE
from chart_manager.plumbing.errors import SpecError
from chart_manager.shared.cluster.session import DEFAULT_CLUSTER_NAME


class ActionKind(StrEnum):
    """What one planned action does."""

    NAMESPACE_ENSURE = "namespace-ensure"
    HELM_LINT = "helm-lint"
    INSTALL = "install"
    #: Wait for a release bootstrap installed, without installing it again.
    WORKLOAD_READY = "workload-ready"
    HELM_TEST = "helm-test"
    HOOK_PRE_INSTALL = "hook-pre-install"
    HOOK_POST_INSTALL = "hook-post-install"
    HOOK_CLEANUP = "hook-cleanup"


@dataclass(frozen=True)
class ActionTarget:
    """Coordinates identifying the subject of one action."""

    chart: str
    profile: str | None = None
    environment: str | None = None
    release: str | None = None
    namespace: str | None = None


@dataclass(frozen=True)
class LifecycleAction:
    """One deterministic, immutable unit of chart-test work."""

    action_id: str
    kind: ActionKind
    target: ActionTarget
    input_digest: str
    chart_path: Path
    values: tuple[Path, ...] = ()
    timeout: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    #: A hook action's argv; empty otherwise.
    command: tuple[str, ...] = ()


@dataclass(frozen=True)
class LifecyclePlan:
    """A deterministic ordered action plan compiled from authored intent."""

    chart: str
    actions: tuple[LifecycleAction, ...]
    profile: str | None = None
    environment: str | None = None
    warnings: tuple[str, ...] = ()


class PlanError(SpecError):
    """The authored charts compile to a plan that cannot run."""


@dataclass(frozen=True)
class ChartTestRequest:
    """One `chart test` invocation."""

    chart: str
    profile: str = DEFAULT_PROFILE
    namespace: str | None = None
    cluster_name: str = DEFAULT_CLUSTER_NAME
    ensure_cluster: bool = True
    include_dependent_tests: bool = False
    skip_requires: bool = False
    lint: bool = False
    run_provision_hooks: bool = True


Verdict = Literal["PASS", "FAIL", "SKIP"]


@dataclass(frozen=True)
class ActionOutcome:
    """How one planned action (or one bootstrap release) ended."""

    action_id: str
    kind: str
    verdict: Verdict
    detail: str | None = None


@dataclass(frozen=True)
class ChartTestOutcome:
    """Every action's verdict in order, and the diagnostics of the one that failed."""

    chart: str
    profile: str
    cluster_name: str
    actions: tuple[ActionOutcome, ...] = ()
    diagnostics: str = ""

    @property
    def failed(self) -> ActionOutcome | None:
        return next((a for a in self.actions if a.verdict == "FAIL"), None)

    @property
    def ok(self) -> bool:
        return self.failed is None


@dataclass(frozen=True)
class TeardownRequest:
    """One `chart teardown` invocation."""

    chart: str
    profile: str = DEFAULT_PROFILE
    namespace: str | None = None
    cluster_name: str = DEFAULT_CLUSTER_NAME
    include_dependent_tests: bool = False
    keep_cluster: bool = False


@dataclass(frozen=True)
class TeardownOutcome:
    """Cleanup verdicts and whether the cluster was deleted."""

    cluster_name: str
    cleanups: tuple[ActionOutcome, ...] = ()
    cluster_deleted: bool = False
    delete_error: str | None = None

    @property
    def failed_cleanups(self) -> tuple[ActionOutcome, ...]:
        return tuple(outcome for outcome in self.cleanups if outcome.verdict == "FAIL")

    @property
    def ok(self) -> bool:
        return not self.failed_cleanups and self.delete_error is None
