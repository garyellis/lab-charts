import pytest

from chart_manager.commands.validate.schemas import lock as schema_lock
from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaLockError,
    KubeconformSchemaSourceEnvironmentError,
)
from chart_manager.commands.validate.schemas.lock import write_schema_lock_atomic
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.shared.workspace import SCHEMA_LOCK_FILE

from .schema_fixtures import schema_store, workspace


def resolver(lock, calls):
    pins = {p.repository: p.resolved for p in (lock.policy.kubernetes, lock.policy.catalog)}

    def resolve_ref(repository, track):
        calls.append((repository, track))
        return pins[repository]

    return resolve_ref


def test_plain_sync_keeps_lock_and_needs_no_charts_even_broken_ones(tmp_path):
    lock, store, snapshots = schema_store(tmp_path)
    write_schema_lock_atomic(tmp_path / SCHEMA_LOCK_FILE, lock)
    before = (tmp_path / SCHEMA_LOCK_FILE).read_bytes()
    result = schema_lock.sync(workspace(tmp_path), store)
    assert result.published
    assert result.lock == lock
    assert len(snapshots.calls) == 2
    # Broken chart data cannot interfere with caching upstream repositories.
    chart = tmp_path / "charts/new/templates/broken.yaml"
    chart.parent.mkdir(parents=True)
    chart.write_text("{{ broken")
    assert not schema_lock.sync(workspace(tmp_path), store).published
    assert (tmp_path / SCHEMA_LOCK_FILE).read_bytes() == before


def test_update_resolves_both_pins_then_publishes_lock(tmp_path):
    lock, store, _ = schema_store(tmp_path)
    calls = []
    result = schema_lock.update(workspace(tmp_path), store, resolve_ref=resolver(lock, calls))
    assert calls == [("yannh/kubernetes-json-schema", "master"), ("datreeio/CRDs-catalog", "main")]
    assert result.lock == lock
    assert (tmp_path / SCHEMA_LOCK_FILE).is_file()
    assert store.inspect(result.lock).ready


def test_failed_update_keeps_previous_lock(tmp_path, monkeypatch):
    lock, store, snapshots = schema_store(tmp_path)
    write_schema_lock_atomic(tmp_path / SCHEMA_LOCK_FILE, lock)
    before = (tmp_path / SCHEMA_LOCK_FILE).read_bytes()

    def failed(*args, **kwargs):
        raise ExternalCommandError("offline")

    monkeypatch.setattr(snapshots, "checkout", failed)
    with pytest.raises(KubeconformSchemaSourceEnvironmentError):
        schema_lock.update(workspace(tmp_path), store, resolve_ref=resolver(lock, []))
    assert (tmp_path / SCHEMA_LOCK_FILE).read_bytes() == before


def test_policy_mismatch_fails_without_download(tmp_path):
    lock, store, snapshots = schema_store(tmp_path)
    write_schema_lock_atomic(
        tmp_path / SCHEMA_LOCK_FILE, lock.model_copy(update={"workspace": "another"})
    )
    with pytest.raises(KubeconformSchemaLockError):
        schema_lock.sync(workspace(tmp_path), store)
    assert not snapshots.calls
