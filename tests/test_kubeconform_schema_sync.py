import pytest

from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaLockError,
    KubeconformSchemaSourceEnvironmentError,
)
from chart_manager.commands.validate.schemas.lock import write_schema_lock_atomic
from chart_manager.commands.validate.schemas.models import AuthoredSchemaPolicy
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.services.kubeconform_schemas.sync import (
    KubeconformSchemaSyncRequest,
    KubeconformSchemaSyncService,
)
from chart_manager.shared.workspace import SCHEMA_LOCK_FILE

from .schema_fixtures import schema_store


class Source:
    def __init__(self, lock):
        self.lock = lock
        self.calls = []

    def resolve_ref(self, repository, ref):
        self.calls.append((repository, ref))
        return next(
            p.resolved
            for p in (self.lock.policy.kubernetes, self.lock.policy.catalog)
            if p.repository == repository
        )


def request(root, *, update=False):
    return KubeconformSchemaSyncRequest(
        workspace="lab",
        lock_path=root / SCHEMA_LOCK_FILE,
        policy=AuthoredSchemaPolicy(
            kubernetes_version="1.35.3",
            generate_from_crds=True,
            catalog_repository="datreeio/CRDs-catalog",
            catalog_track="main",
        ),
        update=update,
    )


def test_plain_sync_never_resolves_refs_or_changes_lock_even_after_chart_changes(tmp_path):
    lock, store, _ = schema_store(tmp_path)
    req = request(tmp_path)
    write_schema_lock_atomic(req.lock_path, lock)
    before = req.lock_path.read_bytes()
    source = Source(lock)
    service = KubeconformSchemaSyncService(store, source)
    assert service.sync(req).generation_published
    # Broken chart data cannot interfere with caching upstream repositories.
    chart = tmp_path / "charts/new/templates/broken.yaml"
    chart.parent.mkdir(parents=True)
    chart.write_text("{{ broken")
    assert not service.sync(req).generation_published
    assert req.lock_path.read_bytes() == before
    assert not source.calls


def test_update_resolves_both_pins_then_publishes_lock(tmp_path):
    lock, store, _ = schema_store(tmp_path)
    source = Source(lock)
    req = request(tmp_path, update=True)
    result = KubeconformSchemaSyncService(store, source).sync(req)
    assert result.lock_updated
    assert len(source.calls) == 2
    assert req.lock_path.is_file()
    assert store.inspect(result.lock).ready


def test_failed_update_keeps_previous_lock(tmp_path, monkeypatch):
    lock, store, snapshots = schema_store(tmp_path)
    req = request(tmp_path, update=True)
    write_schema_lock_atomic(req.lock_path, lock)
    before = req.lock_path.read_bytes()

    def failed(*args, **kwargs):
        raise ExternalCommandError("offline")

    monkeypatch.setattr(snapshots, "checkout", failed)
    with pytest.raises(KubeconformSchemaSourceEnvironmentError):
        KubeconformSchemaSyncService(store, Source(lock)).sync(req)
    assert req.lock_path.read_bytes() == before


def test_policy_mismatch_fails_without_download(tmp_path):
    lock, store, snapshots = schema_store(tmp_path)
    changed = lock.model_copy(update={"workspace": "another"})
    req = request(tmp_path)
    write_schema_lock_atomic(req.lock_path, changed)
    with pytest.raises(KubeconformSchemaLockError):
        KubeconformSchemaSyncService(store, Source(lock)).sync(req)
    assert not snapshots.calls
