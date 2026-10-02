from chart_manager.domain.workspace import SCHEMA_LOCK_FILE
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import CheckStatus
from chart_manager.services.kubeconform_schemas.doctor import KubeconformSchemaDoctor
from chart_manager.services.kubeconform_schemas.lock import write_schema_lock_atomic

from .schema_fixtures import schema_store, workspace


def checks(root, cache):
    return {
        c.name: c for c in KubeconformSchemaDoctor(workspace(root), cache_root=cache).preflight()
    }


def test_doctor_reports_ready_snapshots_without_charts_or_writes(tmp_path):
    lock, store, _ = schema_store(tmp_path)
    path = tmp_path / SCHEMA_LOCK_FILE
    write_schema_lock_atomic(path, lock)
    store.sync(lock)
    before = path.read_bytes()
    result = checks(tmp_path, store.cache_root)
    assert all(c.status is CheckStatus.OK for c in result.values())
    assert result["schema-store"].data["present"] == 2
    assert result["schema-store"].data["ready"]
    assert "automatically" in result["schema-store"].data["generatedSchemas"]
    assert before == path.read_bytes()
    assert not (tmp_path / "charts").exists()


def test_missing_snapshots_are_environmental_and_doctor_does_not_create_cache(tmp_path):
    lock, store, _ = schema_store(tmp_path)
    write_schema_lock_atomic(tmp_path / SCHEMA_LOCK_FILE, lock)
    result = checks(tmp_path, store.cache_root)["schema-store"]
    assert result.outcome is Outcome.ENVIRONMENT
    assert result.data["missing"] == 2
    assert result.remediation == "run chart-manager schemas sync"
    assert not store.root.exists()


def test_corrupt_snapshot_reports_exact_path_without_repair(tmp_path):
    lock, store, _ = schema_store(tmp_path)
    write_schema_lock_atomic(tmp_path / SCHEMA_LOCK_FILE, lock)
    store.sync(lock)
    root = store.repository_path(lock.policy.catalog)
    (root / "example.io/widget_v1.json").write_text("{}")
    result = checks(tmp_path, store.cache_root)["schema-store"]
    assert result.outcome is Outcome.TOOL
    assert str(root) in result.remediation
    assert (root / "example.io/widget_v1.json").read_text() == "{}"


def test_bad_lock_fails_before_store_inspection(tmp_path):
    path = tmp_path / SCHEMA_LOCK_FILE
    path.parent.mkdir(parents=True)
    path.write_text("broken: [\n")
    result = checks(tmp_path, tmp_path / "cache")
    assert result["schema-lock"].outcome is Outcome.SPEC
    assert result["schema-store"].status is CheckStatus.SKIPPED


def test_no_workspace_skips_managed_schema_checks_with_a_hint(tmp_path):
    result = KubeconformSchemaDoctor(None, cache_root=tmp_path / "cache").preflight()
    assert [check.name for check in result] == ["schema-policy", "schema-lock", "schema-store"]
    assert all(check.status is CheckStatus.SKIPPED for check in result)
    assert all("set CHART_MANAGER_ROOT" in check.detail for check in result)
    assert not (tmp_path / "cache").exists()


def test_managed_workspace_still_requires_schema_policy(tmp_path):
    from tests.conftest import workspace_for

    result = KubeconformSchemaDoctor(workspace_for(tmp_path, name="managed")).preflight()
    assert result[0].status is CheckStatus.FAILED
    assert result[0].outcome is Outcome.SPEC
