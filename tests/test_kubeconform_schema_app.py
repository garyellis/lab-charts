from chart_manager.commands.validate.schemas.app import sync
from chart_manager.commands.validate.schemas.lock import write_schema_lock_atomic

from .schema_fixtures import schema_store, workspace
from .test_kubeconform_schema_sync import Source


def test_repository_sync_needs_no_charts_or_renderer(tmp_path):
    lock, store, snapshots = schema_store(tmp_path)
    write_schema_lock_atomic(tmp_path / ".chart-manager/schemas.lock.yaml", lock)
    result = sync(workspace(tmp_path), store=store, source=Source(lock))
    assert result.lock == lock
    assert len(snapshots.calls) == 2
    assert not (tmp_path / "charts").exists()
