"""Shared executor for compiled cluster-test lifecycle action DAGs.

Cluster creation, API-server readiness, and environment-owned bootstrap are
intentionally outside this module.  The executor begins at namespace ensure
and operates only on actions explicitly present in a compiled cluster plan.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from chart_manager.services.lifecycle.evidence import (
    ClusterIdentity,
    EvidenceRecord,
    TargetCoordinates,
)
from chart_manager.services.lifecycle.models import (
    LIFECYCLE_API_VERSION,
    ActionKind,
    LifecycleAction,
    LifecyclePlan,
    Workflow,
)

ActionVerdict = Literal["PASS", "FAIL", "SKIP"]

_SUPPORTED_ACTIONS = frozenset(
    {
        ActionKind.NAMESPACE_ENSURE,
        ActionKind.HELM_DEPENDENCY_UPDATE,
        ActionKind.HELM_UPGRADE_INSTALL,
        ActionKind.WORKLOAD_READY,
        ActionKind.HELM_TEST,
    }
)


class HelmTestResult(Protocol):
    """Minimal Helm test result inspected by the executor."""

    returncode: int
    stdout: str
    stderr: str


class ClusterHelm(Protocol):
    """Helm operations required by compiled cluster actions."""

    def dependency_update_if_stale(self, chart_path: Path) -> object:
        """Refresh chart dependencies when necessary."""

    def upgrade_install(
        self,
        release: str,
        chart_path: Path,
        *,
        namespace: str,
        values: list[Path] | None,
        timeout: str,
        wait: bool,
    ) -> object:
        """Install or converge one release."""

    def test(
        self,
        release: str,
        *,
        namespace: str,
        timeout: str,
    ) -> HelmTestResult:
        """Run Helm tests for one release."""


class ClusterKubectl(Protocol):
    """Kubernetes operations required by compiled cluster actions."""

    def create_namespace(self, namespace: str) -> object:
        """Idempotently ensure a namespace exists."""

    def wait_workloads_ready(
        self,
        namespace: str,
        timeout: str = "10m",
        *,
        selector: str | None = None,
    ) -> object:
        """Wait for workloads in a namespace to become ready."""


class EvidenceSink(Protocol):
    """Append-only evidence sink."""

    def append(self, record: EvidenceRecord) -> Path:
        """Persist one evidence record."""


class ClusterPlanError(ValueError):
    """The supplied plan cannot be executed by the cluster action executor."""


@dataclass(frozen=True)
class ClusterActionOutcome:
    """Complete terminal outcome for one action in execution order."""

    action_id: str
    kind: str
    verdict: ActionVerdict
    reason: str
    detail: str | None
    started_at: datetime
    finished_at: datetime

    @property
    def elapsed_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "actionId": self.action_id,
            "kind": self.kind,
            "verdict": self.verdict,
            "reason": self.reason,
            "detail": self.detail,
            "startedAt": self.started_at.isoformat(),
            "finishedAt": self.finished_at.isoformat(),
            "elapsedSeconds": self.elapsed_seconds,
        }


@dataclass(frozen=True)
class ClusterExecutionDiagnostic:
    """A non-action failure, currently evidence persistence."""

    action_id: str
    stage: Literal["evidence"]
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ClusterExecutionResult:
    """All action outcomes and any non-fatal evidence diagnostics."""

    outcomes: tuple[ClusterActionOutcome, ...]
    evidence_paths: tuple[Path, ...] = ()
    diagnostics: tuple[ClusterExecutionDiagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether every planned action passed."""

        return all(outcome.verdict == "PASS" for outcome in self.outcomes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "apiVersion": LIFECYCLE_API_VERSION,
            "kind": "ClusterExecutionResult",
            "ok": self.ok,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "evidencePaths": [str(path) for path in self.evidence_paths],
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


def _required(value: str | None, action: LifecycleAction, field: str) -> str:
    if value:
        return value
    raise ClusterPlanError(f"action {action.action_id!r} requires target.{field}")


def _target(action: LifecycleAction) -> TargetCoordinates:
    target = action.target
    return TargetCoordinates(
        chart=target.chart,
        workflow=str(target.workflow),
        profile=target.profile,
        environment=target.environment,
        release=target.release,
        namespace=target.namespace,
    )


def _topological_order(plan: LifecyclePlan) -> tuple[
    tuple[LifecycleAction, ...], Mapping[str, frozenset[str]]
]:
    actions_by_id: dict[str, LifecycleAction] = {}
    positions: dict[str, int] = {}
    for position, action in enumerate(plan.actions):
        if action.action_id in actions_by_id:
            raise ClusterPlanError(f"duplicate action id: {action.action_id}")
        actions_by_id[action.action_id] = action
        positions[action.action_id] = position

    successors: dict[str, set[str]] = defaultdict(set)
    predecessors: dict[str, set[str]] = defaultdict(set)
    for edge in plan.edges:
        if edge.source not in actions_by_id:
            raise ClusterPlanError(f"edge source does not exist: {edge.source}")
        if edge.target not in actions_by_id:
            raise ClusterPlanError(f"edge target does not exist: {edge.target}")
        successors[edge.source].add(edge.target)
        predecessors[edge.target].add(edge.source)

    indegree = {action_id: len(predecessors[action_id]) for action_id in actions_by_id}
    ready = [
        positions[action_id] for action_id, degree in indegree.items() if degree == 0
    ]
    heapq.heapify(ready)
    ordered: list[LifecycleAction] = []
    while ready:
        position = heapq.heappop(ready)
        action = plan.actions[position]
        ordered.append(action)
        for successor in sorted(successors[action.action_id], key=positions.__getitem__):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(ready, positions[successor])

    if len(ordered) != len(plan.actions):
        cyclic = sorted(
            (action_id for action_id, degree in indegree.items() if degree),
            key=positions.__getitem__,
        )
        raise ClusterPlanError(f"cluster action graph contains a cycle: {', '.join(cyclic)}")
    return tuple(ordered), {
        action_id: frozenset(required) for action_id, required in predecessors.items()
    }


class ClusterActionExecutor:
    """Execute a compiled cluster-test action graph exactly once per action."""

    def __init__(
        self,
        *,
        helm: ClusterHelm,
        kubectl: ClusterKubectl,
        repository: EvidenceSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.helm = helm
        self.kubectl = kubectl
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    def execute(
        self,
        plan: LifecyclePlan,
        *,
        fail_fast: bool = False,
        run_id: str | None = None,
        cluster: ClusterIdentity | None = None,
    ) -> ClusterExecutionResult:
        """Execute a cluster plan and return a terminal outcome for every action."""

        if plan.workflow is not Workflow.CLUSTER_TEST:
            raise ClusterPlanError(
                f"cluster executor requires workflow {Workflow.CLUSTER_TEST.value!r}, "
                f"got {plan.workflow.value!r}"
            )
        if not plan.actions:
            raise ClusterPlanError("cluster plan contains no actions")
        unsupported = [action for action in plan.actions if action.kind not in _SUPPORTED_ACTIONS]
        if unsupported:
            kinds = ", ".join(sorted({action.kind.value for action in unsupported}))
            raise ClusterPlanError(f"unsupported cluster action kind(s): {kinds}")
        if self.repository is not None and (run_id is None or cluster is None):
            raise ClusterPlanError(
                "run_id and cluster identity are required when evidence recording is enabled"
            )

        ordered, predecessors = _topological_order(plan)
        # Validate coordinates before starting, preventing a late malformed
        # action from leaving an avoidable partial deployment.
        for action in ordered:
            self._validate_coordinates(action)

        outcomes: list[ClusterActionOutcome] = []
        by_id: dict[str, ClusterActionOutcome] = {}
        evidence_paths: list[Path] = []
        diagnostics: list[ClusterExecutionDiagnostic] = []
        failed = False

        for action in ordered:
            prerequisite_failures = [
                predecessor
                for predecessor in predecessors.get(action.action_id, ())
                if by_id[predecessor].verdict != "PASS"
            ]
            if failed and fail_fast:
                outcome = self._skip(action, "FailFast", "an earlier action failed")
            elif prerequisite_failures:
                outcome = self._skip(
                    action,
                    "PrerequisiteFailed",
                    "prerequisite did not pass: " + ", ".join(sorted(prerequisite_failures)),
                )
            else:
                outcome = self._execute_action(action)
                failed = failed or outcome.verdict == "FAIL"

            outcomes.append(outcome)
            by_id[action.action_id] = outcome
            if self.repository is not None:
                assert run_id is not None
                assert cluster is not None
                try:
                    evidence_paths.append(
                        self.repository.append(
                            self._evidence(action, outcome, run_id=run_id, cluster=cluster)
                        )
                    )
                except Exception as exc:
                    diagnostics.append(
                        ClusterExecutionDiagnostic(
                            action_id=action.action_id,
                            stage="evidence",
                            message=str(exc),
                        )
                    )

        return ClusterExecutionResult(
            tuple(outcomes),
            tuple(evidence_paths),
            tuple(diagnostics),
        )

    def _validate_coordinates(self, action: LifecycleAction) -> None:
        if action.target.workflow is not Workflow.CLUSTER_TEST:
            raise ClusterPlanError(
                f"action {action.action_id!r} targets workflow "
                f"{action.target.workflow.value!r}, expected {Workflow.CLUSTER_TEST.value!r}"
            )
        if action.kind is ActionKind.NAMESPACE_ENSURE:
            _required(action.target.namespace, action, "namespace")
        elif action.kind is ActionKind.HELM_UPGRADE_INSTALL:
            _required(action.target.release, action, "release")
            _required(action.target.namespace, action, "namespace")
        elif action.kind is ActionKind.WORKLOAD_READY:
            _required(action.target.namespace, action, "namespace")
            _required(action.target.release, action, "release")
        elif action.kind is ActionKind.HELM_TEST:
            _required(action.target.release, action, "release")
            _required(action.target.namespace, action, "namespace")

    def _execute_action(self, action: LifecycleAction) -> ClusterActionOutcome:
        started_at = self._now()
        try:
            if action.kind is ActionKind.NAMESPACE_ENSURE:
                self.kubectl.create_namespace(
                    _required(action.target.namespace, action, "namespace")
                )
            elif action.kind is ActionKind.HELM_DEPENDENCY_UPDATE:
                self.helm.dependency_update_if_stale(action.chart_path)
            elif action.kind is ActionKind.HELM_UPGRADE_INSTALL:
                self.helm.upgrade_install(
                    _required(action.target.release, action, "release"),
                    action.chart_path,
                    namespace=_required(action.target.namespace, action, "namespace"),
                    values=list(action.values),
                    timeout=action.timeout or "10m",
                    wait=False,
                )
            elif action.kind is ActionKind.WORKLOAD_READY:
                release = _required(action.target.release, action, "release")
                self.kubectl.wait_workloads_ready(
                    _required(action.target.namespace, action, "namespace"),
                    timeout=action.timeout or "10m",
                    selector=f"app.kubernetes.io/instance={release}",
                )
            elif action.kind is ActionKind.HELM_TEST:
                result = self.helm.test(
                    _required(action.target.release, action, "release"),
                    namespace=_required(action.target.namespace, action, "namespace"),
                    timeout=action.timeout or "10m",
                )
                if result.returncode != 0:
                    output = (result.stderr or result.stdout).strip()
                    detail = f"helm test exited {result.returncode}"
                    if output:
                        detail = f"{detail}: {output}"
                    raise RuntimeError(detail)
            else:  # guarded by the preflight check
                raise ClusterPlanError(f"unsupported cluster action kind: {action.kind.value}")
        except Exception as exc:
            return ClusterActionOutcome(
                action_id=action.action_id,
                kind=action.kind.value,
                verdict="FAIL",
                reason="ActionFailed",
                detail=str(exc),
                started_at=started_at,
                finished_at=self._now(),
            )
        return ClusterActionOutcome(
            action_id=action.action_id,
            kind=action.kind.value,
            verdict="PASS",
            reason="ActionCompleted",
            detail=None,
            started_at=started_at,
            finished_at=self._now(),
        )

    def _skip(self, action: LifecycleAction, reason: str, detail: str) -> ClusterActionOutcome:
        observed_at = self._now()
        return ClusterActionOutcome(
            action_id=action.action_id,
            kind=action.kind.value,
            verdict="SKIP",
            reason=reason,
            detail=detail,
            started_at=observed_at,
            finished_at=observed_at,
        )

    def _now(self) -> datetime:
        observed_at = self.clock()
        if observed_at.tzinfo is None:
            raise ClusterPlanError("executor clock must return timezone-aware timestamps")
        return observed_at

    @staticmethod
    def _evidence(
        action: LifecycleAction,
        outcome: ClusterActionOutcome,
        *,
        run_id: str,
        cluster: ClusterIdentity,
    ) -> EvidenceRecord:
        return EvidenceRecord(
            run_id=run_id,
            action_id=action.action_id,
            action_kind=action.kind.value,
            target=_target(action),
            verdict=outcome.verdict,
            status=outcome.verdict,
            reason=outcome.reason,
            detail=outcome.detail,
            input_digest=action.input_digest,
            toolchain={
                key: value
                for key, value in action.metadata
                if key.endswith(("Version", "Binary"))
            },
            cluster=cluster,
            started_at=outcome.started_at,
            finished_at=outcome.finished_at,
            recorded_at=outcome.finished_at,
        )
