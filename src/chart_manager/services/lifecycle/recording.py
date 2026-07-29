"""Adapters that turn validation results into durable lifecycle evidence."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

from chart_manager.services.lifecycle.compiler import LifecycleCompiler
from chart_manager.services.lifecycle.evidence import (
    EvidenceRecord,
    EvidenceStatus,
    EvidenceVerdict,
    LocalEvidenceRepository,
    TargetCoordinates,
)
from chart_manager.services.lifecycle.models import ActionKind, LifecycleAction, LifecyclePlan
from chart_manager.services.manifest_validation.models import (
    PhaseName,
    PhaseResult,
    RunResult,
)
from chart_manager.services.manifest_validation.requests import RunOutcome
from chart_manager.services.manifest_validation.validator_registry import (
    VALIDATOR_REGISTRY,
)
from chart_manager.services.manifest_validation.validators import (
    ValidatorProvider,
    provider_by_id,
    validate_registry,
)
from chart_manager.settings import DEFAULT_CHARTS_DIR

RecordingStage = Literal["compile", "write"]

_PHASE_ACTION_KIND: dict[PhaseName, ActionKind] = {
    "render": ActionKind.RENDER,
    "schema": ActionKind.SCHEMA_VALIDATE,
    "policy": ActionKind.POLICY_VALIDATE,
}


class ValidationPlanCompiler(Protocol):
    """Compiler surface needed by the recorder."""

    def compile_validation(self, chart: str, environment: str) -> LifecyclePlan:
        """Compile the matching validation plan."""


class EvidenceSink(Protocol):
    """Append-only evidence sink used by the recorder."""

    def append(self, record: EvidenceRecord) -> Path:
        """Append a record and return its path."""


@dataclass(frozen=True)
class RecordingDiagnostic:
    """A non-fatal compile or write failure for one validation row/phase."""

    stage: RecordingStage
    chart: str
    environment: str
    message: str
    phase: str | None = None

    def to_dict(self) -> dict[str, str]:
        result = {
            "stage": self.stage,
            "chart": self.chart,
            "environment": self.environment,
            "message": self.message,
        }
        if self.phase is not None:
            result["phase"] = self.phase
        return result


@dataclass(frozen=True)
class ValidationEvidenceRecording:
    """Paths successfully written plus diagnostics for work that was not."""

    run_id: str
    paths: tuple[Path, ...]
    diagnostics: tuple[RecordingDiagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether all eligible evidence records were written."""

        return not self.diagnostics

    def to_dict(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "paths": [str(path) for path in self.paths],
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"validation-{timestamp}-{uuid4().hex[:12]}"


def _safe_run_id(raw: str) -> str:
    """Normalize an injected run identity to the evidence path-safe alphabet."""

    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("._-")
    if not normalized:
        normalized = "validation-run"
    if not normalized[0].isalnum():
        normalized = f"validation-{normalized}"
    return normalized[:128]


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


def _reason(phase: PhaseResult) -> str:
    if phase.error_type == "tool":
        return "ToolError"
    if phase.error_type == "spec":
        return "SpecError"
    return {
        "PASS": "ValidationPassed",
        "FAIL": "ValidationFailed",
        "SKIP": "ValidationSkipped",
        "NOT_RUN": "ValidationNotRun",
    }[phase.status]


def _action_for_phase(plan: LifecyclePlan, phase: PhaseName) -> LifecycleAction:
    kind = _PHASE_ACTION_KIND[phase]
    matches = [action for action in plan.actions if action.kind is kind]
    if len(matches) != 1:
        raise ValueError(
            f"compiled plan has {len(matches)} {kind.value!r} actions; expected exactly one"
        )
    return matches[0]


def _action_for_validator(
    plan: LifecyclePlan,
    validator_id: str,
    providers: tuple[ValidatorProvider, ...],
) -> LifecycleAction:
    """Return the lifecycle action owned by one concrete validator."""
    provider = provider_by_id(validator_id, providers)
    kind = ActionKind(provider.lifecycle_action_kind)
    matches = [action for action in plan.actions if action.kind is kind]
    if len(matches) != 1:
        raise ValueError(
            f"compiled plan has {len(matches)} {kind.value!r} actions; expected exactly one"
        )
    return matches[0]


def _is_authored_validator_skip(plan: LifecyclePlan, phase: PhaseResult) -> bool:
    """Return whether a SKIP corresponds to an intentionally omitted action."""

    if phase.status != "SKIP" or phase.skip_cause != "validator_disabled":
        return False
    kind = _PHASE_ACTION_KIND[phase.phase]
    return not any(action.kind is kind for action in plan.actions)


def _is_authored_identity_skip(
    plan: LifecyclePlan,
    validator_id: str,
    result: PhaseResult,
    providers: tuple[ValidatorProvider, ...],
) -> bool:
    """Return whether an identity-level SKIP has no authored action."""
    if result.status != "SKIP" or result.skip_cause != "validator_disabled":
        return False
    provider = provider_by_id(validator_id, providers)
    kind = ActionKind(provider.lifecycle_action_kind)
    return not any(action.kind is kind for action in plan.actions)


class ManifestValidationEvidenceRecorder:
    """Record terminal manifest-validation phase outcomes as lifecycle evidence."""

    def __init__(
        self,
        root: Path,
        *,
        repository: EvidenceSink | None = None,
        compiler: ValidationPlanCompiler | None = None,
        clock: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[], str] | None = None,
        charts_dir: Path = DEFAULT_CHARTS_DIR,
        validator_providers: tuple[ValidatorProvider, ...] = VALIDATOR_REGISTRY,
    ) -> None:
        self.root = root.resolve()
        self.repository = repository or LocalEvidenceRepository(
            self.root / ".chart-manager" / "state"
        )
        self.compiler = compiler or LifecycleCompiler(
            self.root,
            charts_dir=charts_dir,
            validator_providers=validator_providers,
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.run_id_factory = run_id_factory or _new_run_id
        self.validator_providers = validate_registry(validator_providers)

    def record(self, outcome: RunOutcome | RunResult) -> ValidationEvidenceRecording:
        """Compile each row and append evidence for PASS, FAIL, and SKIP phases.

        ``NOT_RUN`` is intentionally absent: it describes work that did not
        execute, so persisting it as evidence would fabricate an observation.
        A compile or write failure is scoped to its row/phase and does not stop
        independent rows from being recorded.
        """

        result = outcome.result if isinstance(outcome, RunOutcome) else outcome
        run_id = _safe_run_id(self.run_id_factory())
        paths: list[Path] = []
        diagnostics: list[RecordingDiagnostic] = []

        for row_result in result.rows:
            row = row_result.row
            render = row_result.phases.get("render")
            terminal_phases: tuple[
                tuple[str, PhaseResult, str | None],
                ...,
            ]
            if row_result.validator_results:
                concrete = tuple(
                    (
                        definition.category.value,
                        phase,
                        definition.validator_id,
                    )
                    for definition in self.validator_providers
                    if (
                        phase := row_result.validator_results.get(
                            definition.validator_id
                        )
                    )
                    is not None
                    and phase.status != "NOT_RUN"
                )
                terminal_phases = (
                    (
                        (("render", render, None),)
                        if render is not None and render.status != "NOT_RUN"
                        else ()
                    )
                    + concrete
                )
            else:
                terminal_phases = tuple(
                    (phase_name, phase, None)
                    for phase_name in ("render", "schema", "policy")
                    if (phase := row_result.phases.get(phase_name)) is not None
                    and phase.status != "NOT_RUN"
                )
            if not terminal_phases:
                continue
            try:
                plan = self.compiler.compile_validation(row.chart, row.env)
            except Exception as exc:
                diagnostics.append(
                    RecordingDiagnostic(
                        stage="compile",
                        chart=row.chart,
                        environment=row.env,
                        message=str(exc),
                    )
                )
                continue

            for phase_name, phase, validator_id in terminal_phases:
                try:
                    if validator_id is not None and _is_authored_identity_skip(
                        plan,
                        validator_id,
                        phase,
                        self.validator_providers,
                    ):
                        continue
                    if validator_id is None and _is_authored_validator_skip(plan, phase):
                        continue
                    # The collection above excludes the only non-evidence
                    # phase state. Keep the assertion local so static typing
                    # preserves that invariant at the EvidenceRecord boundary.
                    assert phase.status in {"PASS", "FAIL", "SKIP"}
                    verdict = cast(EvidenceVerdict, phase.status)
                    status = cast(EvidenceStatus, phase.status)
                    action = (
                        _action_for_validator(
                            plan,
                            validator_id,
                            self.validator_providers,
                        )
                        if validator_id is not None
                        else _action_for_phase(plan, phase.phase)
                    )
                    finished_at = self.clock()
                    if finished_at.tzinfo is None:
                        raise ValueError("recording clock must return a timezone-aware timestamp")
                    elapsed = max(phase.elapsed_seconds or 0.0, 0.0)
                    record = EvidenceRecord(
                        run_id=run_id,
                        action_id=action.action_id,
                        action_kind=action.kind.value,
                        target=_target(action),
                        verdict=verdict,
                        status=status,
                        reason=_reason(phase),
                        detail=phase.detail,
                        artifacts=tuple(str(path) for path in phase.artifacts),
                        input_digest=action.input_digest,
                        toolchain={
                            key: value
                            for key, value in action.metadata
                            if key.endswith(("Version", "Binary"))
                        },
                        started_at=finished_at - timedelta(seconds=elapsed),
                        finished_at=finished_at,
                        recorded_at=finished_at,
                    )
                    paths.append(self.repository.append(record))
                except Exception as exc:
                    diagnostics.append(
                        RecordingDiagnostic(
                            stage="write",
                            chart=row.chart,
                            environment=row.env,
                            phase=phase_name,
                            message=str(exc),
                        )
                    )

        return ValidationEvidenceRecording(run_id, tuple(paths), tuple(diagnostics))


def record_validation_evidence(
    root: Path,
    outcome: RunOutcome | RunResult,
) -> ValidationEvidenceRecording:
    """Convenience entry point using the default compiler and local repository."""

    return ManifestValidationEvidenceRecorder(root).record(outcome)
