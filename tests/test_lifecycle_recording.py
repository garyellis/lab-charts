from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from chart_manager.services.lifecycle.evidence import LocalEvidenceRepository
from chart_manager.services.lifecycle.models import (
    ActionKind,
    ActionTarget,
    LifecycleAction,
    LifecyclePlan,
    Workflow,
)
from chart_manager.services.lifecycle.recording import ManifestValidationEvidenceRecorder
from chart_manager.services.lifecycle.status import project_status
from chart_manager.services.manifest_validation.models import (
    PhaseResult,
    RowResult,
    RunResult,
    WorklistRow,
)
from chart_manager.services.manifest_validation.requests import RunOutcome

NOW = datetime(2026, 7, 26, 18, tzinfo=UTC)


def validation_plan(
    chart: str,
    environment: str,
    *,
    digest_suffix: str = "v1",
) -> LifecyclePlan:
    target = ActionTarget(
        workflow=Workflow.VALIDATION,
        chart=chart,
        environment=environment,
        release=chart,
        namespace=f"lab-{environment}",
    )
    definitions = (
        (ActionKind.HELM_DEPENDENCY_UPDATE, "dependency-update"),
        (ActionKind.RENDER, "render"),
        (ActionKind.SCHEMA_VALIDATE, "schema"),
        (ActionKind.POLICY_VALIDATE, "policy"),
    )
    actions = tuple(
        LifecycleAction(
            action_id=f"validation:{chart}:{environment}:{suffix}",
            kind=kind,
            target=target,
            input_digest=f"{suffix}-{digest_suffix}",
            chart_path=Path("charts") / chart,
            metadata=(("helmVersion", "3.18.4"),),
        )
        for kind, suffix in definitions
    )
    return LifecyclePlan(
        workflow=Workflow.VALIDATION,
        chart=chart,
        environment=environment,
        actions=actions,
        edges=(),
    )


class StubCompiler:
    def __init__(
        self,
        plans: dict[tuple[str, str], LifecyclePlan | Exception],
    ) -> None:
        self.plans = plans

    def compile_validation(self, chart: str, environment: str) -> LifecyclePlan:
        result = self.plans[(chart, environment)]
        if isinstance(result, Exception):
            raise result
        return result


def row_result(
    chart: str,
    environment: str,
    *,
    render: PhaseResult,
    schema: PhaseResult,
    policy: PhaseResult,
) -> RowResult:
    return RowResult(
        row=WorklistRow(
            chart=chart,
            env=environment,
            release=chart,
            namespace=f"lab-{environment}",
        ),
        phases={"render": render, "schema": schema, "policy": policy},
    )


def test_records_pass_fail_and_skip_but_not_not_run_or_dependency_update(
    tmp_path: Path,
) -> None:
    plan = validation_plan("grafana", "dev")
    repository = LocalEvidenceRepository(tmp_path / "state")
    first = row_result(
        "grafana",
        "dev",
        render=PhaseResult(
            phase="render",
            status="PASS",
            detail="rendered",
            artifacts=(Path("rendered.yaml"),),
            elapsed_seconds=2.5,
        ),
        schema=PhaseResult(
            phase="schema",
            status="FAIL",
            detail="invalid replicas",
            artifacts=(Path("schema.json"),),
            elapsed_seconds=0.5,
        ),
        policy=PhaseResult(
            phase="policy",
            status="SKIP",
            detail="no policies discovered",
        ),
    )
    second = row_result(
        "grafana",
        "dev",
        render=PhaseResult(phase="render", status="NOT_RUN"),
        schema=PhaseResult(phase="schema", status="NOT_RUN"),
        policy=PhaseResult(phase="policy", status="NOT_RUN"),
    )
    outcome = RunOutcome(
        result=RunResult(rows=(first, second), rendered_root=tmp_path / "rendered"),
        out_dir=tmp_path / "rendered",
    )
    recorder = ManifestValidationEvidenceRecorder(
        tmp_path,
        repository=repository,
        compiler=StubCompiler({("grafana", "dev"): plan}),
        clock=lambda: NOW,
        run_id_factory=lambda: "validation test/run",
    )

    result = recorder.record(outcome)
    history = repository.history()

    assert result.ok
    assert result.run_id == "validation-test-run"
    assert len(result.paths) == 3
    assert {record.verdict for record in history.records} == {"PASS", "FAIL", "SKIP"}
    assert {record.status for record in history.records} == {"PASS", "FAIL", "SKIP"}
    assert {record.action_kind for record in history.records} == {
        "render",
        "schema-validate",
        "policy-validate",
    }
    assert all(record.run_id == result.run_id for record in history.records)
    assert all("dependency-update" not in record.action_id for record in history.records)
    render = next(record for record in history.records if record.action_kind == "render")
    assert render.detail == "rendered"
    assert render.artifacts == ("rendered.yaml",)
    assert render.elapsed_seconds == 2.5
    assert render.toolchain == {"helmVersion": "3.18.4"}


def test_compile_failure_does_not_prevent_an_independent_row_from_recording(
    tmp_path: Path,
) -> None:
    repository = LocalEvidenceRepository(tmp_path / "state")
    failed = row_result(
        "broken",
        "dev",
        render=PhaseResult(phase="render", status="PASS"),
        schema=PhaseResult(phase="schema", status="PASS"),
        policy=PhaseResult(phase="policy", status="PASS"),
    )
    healthy = row_result(
        "grafana",
        "prod",
        render=PhaseResult(phase="render", status="PASS"),
        schema=PhaseResult(phase="schema", status="NOT_RUN"),
        policy=PhaseResult(phase="policy", status="NOT_RUN"),
    )
    recorder = ManifestValidationEvidenceRecorder(
        tmp_path,
        repository=repository,
        compiler=StubCompiler(
            {
                ("broken", "dev"): ValueError("invalid chart configuration"),
                ("grafana", "prod"): validation_plan("grafana", "prod"),
            }
        ),
        clock=lambda: NOW,
        run_id_factory=lambda: "run-1",
    )

    result = recorder.record(RunResult(rows=(failed, healthy), rendered_root=tmp_path))

    assert not result.ok
    assert len(result.paths) == 1
    assert result.diagnostics[0].to_dict() == {
        "stage": "compile",
        "chart": "broken",
        "environment": "dev",
        "message": "invalid chart configuration",
    }
    assert repository.history().records[0].target.chart == "grafana"


def test_disabled_validator_skip_needs_no_action_or_evidence_record(
    tmp_path: Path,
) -> None:
    full_plan = validation_plan("grafana", "dev")
    plan = replace(
        full_plan,
        actions=tuple(
            action
            for action in full_plan.actions
            if action.kind is not ActionKind.SCHEMA_VALIDATE
        ),
    )
    repository = LocalEvidenceRepository(tmp_path / "state")
    result = row_result(
        "grafana",
        "dev",
        render=PhaseResult(phase="render", status="PASS"),
        schema=PhaseResult(
            phase="schema",
            status="SKIP",
            detail="disabled by chart-lifecycle",
            skip_cause="validator_disabled",
        ),
        policy=PhaseResult(phase="policy", status="PASS"),
    )
    recorder = ManifestValidationEvidenceRecorder(
        tmp_path,
        repository=repository,
        compiler=StubCompiler({("grafana", "dev"): plan}),
        clock=lambda: NOW,
        run_id_factory=lambda: "run-1",
    )

    recording = recorder.record(RunResult(rows=(result,), rendered_root=tmp_path))

    assert recording.ok
    assert len(recording.paths) == 2
    assert {record.action_kind for record in repository.history().records} == {
        "render",
        "policy-validate",
    }


def test_disabled_validators_need_no_actions_when_render_fails(tmp_path: Path) -> None:
    full_plan = validation_plan("grafana", "dev")
    plan = replace(
        full_plan,
        actions=tuple(
            action
            for action in full_plan.actions
            if action.kind
            not in {ActionKind.SCHEMA_VALIDATE, ActionKind.POLICY_VALIDATE}
        ),
    )
    repository = LocalEvidenceRepository(tmp_path / "state")
    result = row_result(
        "grafana",
        "dev",
        render=PhaseResult(phase="render", status="FAIL", error_type="tool"),
        schema=PhaseResult(
            phase="schema",
            status="SKIP",
            skip_cause="validator_disabled",
        ),
        policy=PhaseResult(
            phase="policy",
            status="SKIP",
            skip_cause="validator_disabled",
        ),
    )
    recorder = ManifestValidationEvidenceRecorder(
        tmp_path,
        repository=repository,
        compiler=StubCompiler({("grafana", "dev"): plan}),
        clock=lambda: NOW,
        run_id_factory=lambda: "run-1",
    )

    recording = recorder.record(RunResult(rows=(result,), rendered_root=tmp_path))

    assert recording.ok
    assert len(recording.paths) == 1
    assert repository.history().records[0].action_kind == "render"


def test_disabled_policy_needs_no_action_when_schema_fails(tmp_path: Path) -> None:
    full_plan = validation_plan("grafana", "dev")
    plan = replace(
        full_plan,
        actions=tuple(
            action
            for action in full_plan.actions
            if action.kind is not ActionKind.POLICY_VALIDATE
        ),
    )
    repository = LocalEvidenceRepository(tmp_path / "state")
    result = row_result(
        "grafana",
        "dev",
        render=PhaseResult(phase="render", status="PASS"),
        schema=PhaseResult(phase="schema", status="FAIL"),
        policy=PhaseResult(
            phase="policy",
            status="SKIP",
            detail="wording is presentation-only",
            skip_cause="validator_disabled",
        ),
    )
    recorder = ManifestValidationEvidenceRecorder(
        tmp_path,
        repository=repository,
        compiler=StubCompiler({("grafana", "dev"): plan}),
        clock=lambda: NOW,
        run_id_factory=lambda: "run-1",
    )

    recording = recorder.record(RunResult(rows=(result,), rendered_root=tmp_path))

    assert recording.ok
    assert len(recording.paths) == 2
    assert {record.action_kind for record in repository.history().records} == {
        "render",
        "schema-validate",
    }


def test_missing_enabled_validator_action_remains_a_diagnostic(tmp_path: Path) -> None:
    full_plan = validation_plan("grafana", "dev")
    plan = replace(
        full_plan,
        actions=tuple(
            action
            for action in full_plan.actions
            if action.kind is not ActionKind.POLICY_VALIDATE
        ),
    )
    repository = LocalEvidenceRepository(tmp_path / "state")
    result = row_result(
        "grafana",
        "dev",
        render=PhaseResult(phase="render", status="FAIL", error_type="tool"),
        schema=PhaseResult(
            phase="schema",
            status="SKIP",
            skip_cause="upstream_failed",
        ),
        policy=PhaseResult(
            phase="policy",
            status="SKIP",
            skip_cause="upstream_failed",
        ),
    )
    recorder = ManifestValidationEvidenceRecorder(
        tmp_path,
        repository=repository,
        compiler=StubCompiler({("grafana", "dev"): plan}),
        clock=lambda: NOW,
        run_id_factory=lambda: "run-1",
    )

    recording = recorder.record(RunResult(rows=(result,), rendered_root=tmp_path))

    assert not recording.ok
    assert len(recording.paths) == 2
    assert recording.diagnostics[0].phase == "policy"
    assert "expected exactly one" in recording.diagnostics[0].message


def test_recorded_digest_projects_stale_after_plan_inputs_change(tmp_path: Path) -> None:
    original = validation_plan("grafana", "dev", digest_suffix="v1")
    repository = LocalEvidenceRepository(tmp_path / "state")
    result = RunResult(
        rows=(
            row_result(
                "grafana",
                "dev",
                render=PhaseResult(phase="render", status="PASS"),
                schema=PhaseResult(phase="schema", status="NOT_RUN"),
                policy=PhaseResult(phase="policy", status="NOT_RUN"),
            ),
        ),
        rendered_root=tmp_path,
    )
    recorder = ManifestValidationEvidenceRecorder(
        tmp_path,
        repository=repository,
        compiler=StubCompiler({("grafana", "dev"): original}),
        clock=lambda: NOW,
        run_id_factory=lambda: "run-1",
    )
    recorder.record(result)
    changed_render = replace(
        original.actions[1],
        input_digest="render-v2",
    )
    changed_plan = replace(
        original,
        actions=(
            original.actions[0],
            changed_render,
            original.actions[2],
            original.actions[3],
        ),
    )

    status = project_status(changed_plan, repository.history())

    render_status = next(row for row in status.actions if row.kind == "render")
    assert render_status.freshness == "stale"
    assert render_status.verdict == "PASS"
    assert render_status.expected_input_digest == "render-v2"
