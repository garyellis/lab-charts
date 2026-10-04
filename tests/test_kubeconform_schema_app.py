from chart_manager.commands.validate.schemas.lock import write_schema_lock_atomic
from chart_manager.services.kubeconform_schemas.app import RepositoryKubeconformSchemaService
from chart_manager.services.kubeconform_schemas.sync import KubeconformSchemaSyncService

from .schema_fixtures import schema_store, workspace
from .test_kubeconform_schema_sync import Source


def test_repository_sync_needs_no_charts_or_renderer(tmp_path):
    lock, store, snapshots = schema_store(tmp_path)
    write_schema_lock_atomic(tmp_path / ".chart-manager/schemas.lock.yaml", lock)
    service = RepositoryKubeconformSchemaService(
        workspace=workspace(tmp_path), sync=KubeconformSchemaSyncService(store, Source(lock))
    )
    result = service.sync()
    assert result.lock == lock
    assert len(snapshots.calls) == 2
    assert not (tmp_path / "charts").exists()
