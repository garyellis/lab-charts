import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chart_manager.services.lifecycle.evidence import (
    EVIDENCE_API_VERSION,
    ClusterIdentity,
    EvidenceRecord,
    EvidenceSource,
    EvidenceVerdict,
    LocalEvidenceRepository,
    TargetCoordinates,
)
from chart_manager.services.lifecycle.models import (
    ActionKind,
    ActionTarget,
    LifecycleAction,
    LifecyclePlan,
    Workflow,
)
from chart_manager.services.lifecycle.status import project_status


@dataclass(frozen=True)
class StubTarget:
    chart: str
    profile: str
    environment: str | None = None
    release: str | None = None
    namespace: str | None = None


@dataclass(frozen=True)
class StubAction:
    action_id: str
    kind: str
    target: StubTarget
    input_digest: str


@dataclass(frozen=True)
class StubPlan:
    workflow: str
    actions: tuple[StubAction, ...]


NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)
TARGET = TargetCoordinates(chart="grafana", workflow="validate", profile="production")


def evidence(
    *,
    evidence_id: str,
    action_id: str = "grafana.validate.production.render",
    action_kind: str = "render",
    digest: str = "digest-a",
    verdict: EvidenceVerdict = "PASS",
    source: EvidenceSource = "local",
    recorded_at: datetime = NOW,
    target: TargetCoordinates = TARGET,
    cluster: ClusterIdentity | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        run_id="run-1",
        action_id=action_id,
        action_kind=action_kind,
        target=target,
        verdict=verdict,
        status=verdict,
        reason="ToolExited",
        detail="focused evidence fixture",
        input_digest=digest,
        source=source,
        cluster=cluster,
        started_at=recorded_at - timedelta(seconds=2),
        finished_at=recorded_at - timedelta(seconds=1),
        recorded_at=recorded_at,
        artifacts=("rendered.yaml",),
        toolchain={"helm": "3.18.4"},
    )


def plan(*actions: StubAction) -> StubPlan:
    return StubPlan("validate", actions)


def action(
    *,
    action_id: str = "grafana.validate.production.render",
    kind: str = "render",
    digest: str = "digest-a",
) -> StubAction:
    return StubAction(
        action_id=action_id,
        kind=kind,
        target=StubTarget(chart="grafana", profile="production"),
        input_digest=digest,
    )


def test_evidence_round_trips_through_atomic_repository(tmp_path: Path) -> None:
    repository = LocalEvidenceRepository(tmp_path / ".chart-manager" / "state")
    record = evidence(evidence_id="evidence-1")

    path = repository.append(record)
    result = repository.latest(action_id=record.action_id, target=TARGET)

    assert path.name == "evidence-1.json"
    assert result.record == record
    assert result.diagnostics == ()
    assert result.record is not None
    assert result.record.to_dict()["apiVersion"] == EVIDENCE_API_VERSION
    assert result.record.to_dict()["elapsedSeconds"] == 1.0


def test_repository_reports_corrupt_records_without_hiding_valid_history(tmp_path: Path) -> None:
    repository = LocalEvidenceRepository(tmp_path)
    repository.append(evidence(evidence_id="valid"))
    corrupt = repository.records_dir / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")

    history = repository.history()

    assert [record.evidence_id for record in history.records] == ["valid"]
    assert len(history.diagnostics) == 1
    assert history.diagnostics[0].path == corrupt
    assert "Expecting property name" in history.diagnostics[0].message


def test_record_identity_cannot_be_used_for_path_traversal() -> None:
    with pytest.raises(ValueError, match="evidence_id is not path-safe"):
        evidence(evidence_id="../../outside")


@pytest.mark.parametrize(
    ("field", "value"),
    (("verdict", "ERROR"), ("status", "completed")),
)
def test_evidence_rejects_unknown_verdict_and_status(field: str, value: str) -> None:
    values = evidence(evidence_id="base").to_dict()
    values[field] = value

    with pytest.raises(ValueError, match=f"unsupported evidence {field}"):
        EvidenceRecord.from_dict(values)


def test_repository_rejects_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="root must not contain symlinks"):
        LocalEvidenceRepository(linked)


def test_repository_rejects_symlink_introduced_before_append(tmp_path: Path) -> None:
    root = tmp_path / "state"
    repository = LocalEvidenceRepository(root)
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    repository.evidence_dir.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="contains a symlink"):
        repository.append(evidence(evidence_id="blocked"))
    assert list(external.iterdir()) == []


def test_repository_does_not_read_through_evidence_symlink(tmp_path: Path) -> None:
    root = tmp_path / "state"
    repository = LocalEvidenceRepository(root)
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    repository.evidence_dir.symlink_to(external, target_is_directory=True)

    history = repository.history()

    assert history.records == ()
    assert len(history.diagnostics) == 1
    assert "symlink" in history.diagnostics[0].message


def test_projection_distinguishes_current_stale_and_unknown() -> None:
    current_action = action()
    stale_action = action(
        action_id="grafana.validate.production.schema", kind="schema", digest="digest-new"
    )
    unknown_action = action(
        action_id="grafana.validate.production.policy", kind="policy", digest="digest-policy"
    )
    status = project_status(
        plan(current_action, stale_action, unknown_action),
        (
            evidence(evidence_id="current"),
            evidence(
                evidence_id="old-schema",
                action_id=stale_action.action_id,
                action_kind="schema",
                digest="digest-old",
            ),
        ),
    )

    assert [row.freshness for row in status.actions] == ["current", "stale", "unknown"]
    assert {condition.type: condition.status for condition in status.conditions} == {
        "policy": "UNKNOWN",
        "render": "PASS",
        "schema": "STALE",
    }


def test_matching_digest_remains_current_even_when_stale_record_is_newer() -> None:
    expected = action()
    status = project_status(
        plan(expected),
        (
            evidence(evidence_id="matching", recorded_at=NOW),
            evidence(
                evidence_id="newer-but-stale",
                digest="digest-b",
                verdict="FAIL",
                recorded_at=NOW + timedelta(minutes=1),
            ),
        ),
    )

    assert status.actions[0].freshness == "current"
    assert status.actions[0].verdict == "PASS"
    assert status.actions[0].source is not None
    assert status.actions[0].source.evidence_id == "matching"


def test_live_observation_wins_timestamp_tie_and_is_labeled_live() -> None:
    cached = evidence(evidence_id="cached", verdict="FAIL", recorded_at=NOW)
    live = evidence(evidence_id="observed", verdict="PASS", source="live", recorded_at=NOW)

    status = project_status(plan(action()), (cached,), live_observations=(live,))

    assert status.actions[0].verdict == "PASS"
    assert status.actions[0].source is not None
    assert status.actions[0].source.origin == "live"


def test_live_unknown_beats_newer_cached_pass() -> None:
    cached = evidence(
        evidence_id="cached",
        recorded_at=NOW + timedelta(minutes=10),
    )
    live = evidence(
        evidence_id="observed-missing",
        verdict="UNKNOWN",
        source="live",
        recorded_at=NOW,
    )

    status = project_status(plan(action()), (cached,), live_observations=(live,))

    assert status.actions[0].verdict == "UNKNOWN"
    assert status.actions[0].source is not None
    assert status.actions[0].source.origin == "live"
    assert status.conditions[0].status == "UNKNOWN"


def test_cluster_test_cache_is_scoped_to_requested_cluster() -> None:
    cluster_a = ClusterIdentity(name="kind-a", context="kind-a", uid="uid-a")
    cluster_b = ClusterIdentity(name="kind-b", context="kind-b", uid="uid-b")
    cluster_target = TargetCoordinates(
        chart="grafana",
        workflow="cluster-test",
        profile="production",
    )
    cluster_plan = StubPlan("cluster-test", (action(),))
    cached = evidence(
        evidence_id="cluster-a",
        target=cluster_target,
        cluster=cluster_a,
    )

    status = project_status(
        cluster_plan,
        (cached,),
        requested_cluster=cluster_b,
    )

    assert status.actions[0].freshness == "unknown"
    assert status.conditions[0].status == "UNKNOWN"


def test_persisted_live_sourced_record_is_still_labeled_cached() -> None:
    previously_live = evidence(evidence_id="previous-observation", source="live")

    status = project_status(plan(action()), (previously_live,))

    assert status.actions[0].source is not None
    assert status.actions[0].source.source == "live"
    assert status.actions[0].source.origin == "cached"


def test_status_with_evidence_is_json_serializable() -> None:
    status = project_status(
        plan(action()),
        (evidence(evidence_id="serializable"),),
    )

    payload = status.to_dict()

    assert json.loads(json.dumps(payload)) == payload
    source = payload["actions"][0]["source"]
    assert source["observedAt"] == NOW.isoformat()
    assert "observed_at" not in source


def test_summary_condition_reports_current_failure_without_boolean_collapse() -> None:
    failed = evidence(evidence_id="failed", verdict="FAIL")

    status = project_status(plan(action()), (failed,))

    assert status.conditions[0].to_dict() == {
        "type": "render",
        "status": "FAIL",
        "reason": "CurrentEvidenceFailed",
        "current": 1,
        "stale": 0,
        "unknown": 0,
        "failed": 1,
        "skipped": 0,
        "total": 1,
    }
    serialized = status.to_dict()
    assert serialized["apiVersion"] == "lifecycle.cmg.io/v1alpha1"
    assert serialized["kind"] == "LifecycleStatus"
    assert "ready" not in serialized


def test_projection_accepts_compiled_validation_plan_coordinates() -> None:
    compiled_action = LifecycleAction(
        action_id="grafana.validate.dev.render",
        kind=ActionKind.RENDER,
        target=ActionTarget(
            workflow=Workflow.VALIDATE,
            chart="grafana",
            environment="dev",
            release="grafana",
            namespace="monitoring",
        ),
        input_digest="digest-dev",
        chart_path=Path("charts/grafana"),
    )
    compiled_plan = LifecyclePlan(
        workflow=Workflow.VALIDATE,
        chart="grafana",
        environment="dev",
        actions=(compiled_action,),
        edges=(),
    )

    status = project_status(compiled_plan, ())

    assert status.actions[0].target == TargetCoordinates(
        chart="grafana",
        workflow="validation",
        environment="dev",
        release="grafana",
        namespace="monitoring",
    )
    assert status.actions[0].kind == "render"


def test_all_current_skips_are_unknown_not_passed() -> None:
    skipped = evidence(evidence_id="skipped", verdict="SKIP")

    status = project_status(plan(action()), (skipped,))

    assert status.conditions[0].status == "UNKNOWN"
    assert status.conditions[0].reason == "ActionsSkipped"
    assert status.conditions[0].skipped == 1


def test_current_pass_and_skip_are_a_mixed_condition() -> None:
    first = action(action_id="grafana.validate.production.render.first")
    second = action(action_id="grafana.validate.production.render.second")
    passed = evidence(evidence_id="passed", action_id=first.action_id)
    skipped = evidence(
        evidence_id="skipped",
        action_id=second.action_id,
        verdict="SKIP",
    )

    status = project_status(plan(first, second), (passed, skipped))

    assert status.conditions[0].status == "MIXED"
    assert status.conditions[0].reason == "IncompleteStaleOrSkippedEvidence"
