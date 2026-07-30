"""Shared executor for compiled cluster-test lifecycle action plans.

Cluster creation, API-server readiness, and environment-owned bootstrap are
intentionally outside this module.  The executor begins at namespace ensure
and operates only on actions explicitly present in a compiled cluster plan.
"""

from __future__ import annotations

from collections.abc import Callable
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
from chart_manager.services.progress import (
    ProgressCallback,
    detail,
    emit,
    failure,
    step,
)

ActionVerdict = Literal["PASS", "FAIL", "SKIP"]

_SUPPORTED_ACTIONS = frozenset(
    {
        ActionKind.NAMESPACE_ENSURE,
        ActionKind.HELM_DEPENDENCY_UPDATE,
        ActionKind.HELM_LINT,
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

    def lint(self, chart_path: Path, values: list[Path] | None = None) -> None:
        """Validate one chart with its selected values."""

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


class ClusterActionExecutor:
    """Execute a compiled cluster-test action plan exactly once per action."""

    def __init__(
        self,
        *,
        helm: ClusterHelm,
        kubectl: ClusterKubectl,
        repository: EvidenceSink | None = None,
        clock: Callable[[], datetime] | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.helm = helm
        self.kubectl = kubectl
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))
        self.progress = progress

    def execute(
        self,
        plan: LifecyclePlan,
        *,
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
        action_ids: set[str] = set()
        for action in plan.actions:
            if action.action_id in action_ids:
                raise ClusterPlanError(f"duplicate action id: {action.action_id}")
            action_ids.add(action.action_id)
        unsupported = [action for action in plan.actions if action.kind not in _SUPPORTED_ACTIONS]
        if unsupported:
            kinds = ", ".join(sorted({action.kind.value for action in unsupported}))
            raise ClusterPlanError(f"unsupported cluster action kind(s): {kinds}")
        if self.repository is not None and (run_id is None or cluster is None):
            raise ClusterPlanError(
                "run_id and cluster identity are required when evidence recording is enabled"
            )

        # Validate coordinates before starting, preventing a late malformed
        # action from leaving an avoidable partial deployment.
        for action in plan.actions:
            self._validate_coordinates(action)

        outcomes: list[ClusterActionOutcome] = []
        evidence_paths: list[Path] = []
        diagnostics: list[ClusterExecutionDiagnostic] = []
        failed = False

        for action in plan.actions:
            if failed:
                outcome = self._skip(action, "FailFast", "an earlier action failed")
                emit(
                    self.progress,
                    detail("Skipped", self._progress_subject(action)),
                )
            else:
                subject = self._progress_subject(action)
                emit(self.progress, step(self._progress_label(action), subject))
                outcome = self._execute_action(action)
                failed = failed or outcome.verdict == "FAIL"
                if outcome.verdict == "PASS":
                    emit(self.progress, detail("Completed", subject))
                else:
                    emit(
                        self.progress,
                        failure(
                            "Failed",
                            f"{subject}: {outcome.detail or 'unknown error'}",
                        ),
                    )

            outcomes.append(outcome)
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
            elif action.kind is ActionKind.HELM_LINT:
                self.helm.lint(action.chart_path, list(action.values))
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
    def _progress_subject(action: LifecycleAction) -> str:
        profile = f":{action.target.profile}" if action.target.profile else ""
        namespace = (
            f" in {action.target.namespace}" if action.target.namespace is not None else ""
        )
        return f"{action.target.chart}{profile}{namespace}"

    @staticmethod
    def _progress_label(action: LifecycleAction) -> str:
        return {
            ActionKind.NAMESPACE_ENSURE: "Ensuring namespace",
            ActionKind.HELM_DEPENDENCY_UPDATE: "Updating dependencies",
            ActionKind.HELM_LINT: "Linting",
            ActionKind.HELM_UPGRADE_INSTALL: "Installing",
            ActionKind.WORKLOAD_READY: "Waiting for workloads",
            ActionKind.HELM_TEST: "Running Helm tests",
        }[action.kind]

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
