from pathlib import Path

import pytest

from chart_manager.commands.validate.schemas.errors import KubeconformSchemaStoreError
from chart_manager.plumbing.schema_locations import expand_schema_location

from .schema_fixtures import git, schema_store


def test_sync_caches_complete_selected_version_and_catalog_once(tmp_path):
    lock, store, snapshots = schema_store(tmp_path)
    assert store.sync(lock)
    assert len(snapshots.calls) == 2
    assert store.inspect(lock).ready
    before = snapshots.calls.copy()
    assert not store.sync(lock)
    assert snapshots.calls == before
    kubernetes = store.repository_path(lock.policy.kubernetes, "v1.35.3-standalone-strict")
    assert not (kubernetes / "v1.34.0-standalone-strict").exists()
    # A kind never inventoried by any chart is already available.
    location = store.locations(lock)[0]
    path = expand_schema_location(
        location, group="policy", version="v1", kind="PodDisruptionBudget"
    )
    assert Path(path).is_file()
    catalog = store.locations(lock)[1]
    assert Path(
        expand_schema_location(catalog, group="example.io", version="v1", kind="Widget")
    ).is_file()


@pytest.mark.parametrize(
    "damage",
    ["modify", "delete", "extra", "symlink", "staged", "assume-unchanged", "skip-worktree"],
)
def test_inspection_verifies_schema_bytes_against_pinned_tree(tmp_path, damage):
    lock, store, _ = schema_store(tmp_path)
    store.sync(lock)
    root = store.repository_path(lock.policy.kubernetes, "v1.35.3-standalone-strict")
    path = root / "v1.35.3-standalone-strict/configmap-v1.json"
    if damage == "extra":
        (path.parent / "unlocked-v1.json").write_text("{}")
    elif damage == "delete":
        path.unlink()
    elif damage == "symlink":
        external = tmp_path / "outside.json"
        external.write_text(path.read_text())
        path.unlink()
        path.symlink_to(external)
    else:
        path.write_text("{}")
        if damage == "staged":
            git(root, "add", ".")
        if damage in {"assume-unchanged", "skip-worktree"}:
            git(root, "update-index", "--" + damage, str(path.relative_to(root)))
    status = store.inspect(lock)
    assert not status.ready
    assert len(status.corrupt) == 1
    with pytest.raises(KubeconformSchemaStoreError, match="remove this snapshot"):
        store.sync(lock)


def test_failed_checkout_is_not_published_and_can_retry(tmp_path, monkeypatch):
    lock, store, snapshots = schema_store(tmp_path)
    original = snapshots.checkout

    def failed(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(snapshots, "checkout", failed)
    with pytest.raises(KubeconformSchemaStoreError, match="disk full"):
        store.sync(lock)
    assert not store.inspect(lock).ready
    assert not list(store.root.rglob(".staging-*"))
    monkeypatch.setattr(snapshots, "checkout", original)
    assert store.sync(lock)
    assert store.inspect(lock).ready


def test_empty_store_inspection_does_not_create_directories(tmp_path):
    lock, store, _ = schema_store(tmp_path)
    status = store.inspect(lock)
    assert len(status.missing) == 2
    assert not store.root.exists()


def test_partial_sparse_checkout_materializes_all_selected_blobs_for_offline_use(tmp_path):
    from chart_manager.integrations.kubeconform.repository_snapshot import RepositorySnapshot
    from chart_manager.plumbing.commands import SubprocessRunner

    lock, _, sources = schema_store(tmp_path)
    upstream = sources.repositories[lock.policy.kubernetes.repository]
    git(upstream, "config", "uploadpack.allowFilter", "true")
    calls = []

    class LocalTransport:
        def run(self, args, **kwargs):
            args = list(args)
            calls.append((args.copy(), kwargs.get("env", {})))
            if "remote" in args and "add" in args:
                args[-1] = upstream.as_uri()
            return SubprocessRunner().run(args, **kwargs)

    snapshots = RepositorySnapshot(LocalTransport(), timeout=None)
    destination = tmp_path / "snapshot"
    snapshots.checkout(
        lock.policy.kubernetes.repository,
        lock.policy.kubernetes.resolved,
        destination,
        directory="v1.35.3-standalone-strict",
    )
    assert any("--filter=blob:none" in args for args, _ in calls)
    assert not (destination / "v1.34.0-standalone-strict").exists()
    from chart_manager.integrations.kubeconform.repository_snapshot import (
        RepositorySnapshotDirectoryNotFoundError,
    )

    with pytest.raises(RepositorySnapshotDirectoryNotFoundError, match="no schema directory"):
        snapshots.checkout(
            lock.policy.kubernetes.repository,
            lock.policy.kubernetes.resolved,
            tmp_path / "missing-version",
            directory="v9.99.0-standalone-strict",
        )
    # Make the only origin unreachable; checking and reading schemas still works.
    git(destination, "remote", "set-url", "origin", (tmp_path / "absent").as_uri())
    calls.clear()
    assert (
        snapshots.inspect(
            destination, lock.policy.kubernetes.resolved, directory="v1.35.3-standalone-strict"
        )
        is None
    )
    assert all(env["GIT_NO_LAZY_FETCH"] == "1" for _, env in calls)
    assert (
        destination / "v1.35.3-standalone-strict/poddisruptionbudget-policy-v1.json"
    ).read_text()


def test_unwritable_cache_reports_a_store_error(tmp_path):
    lock, store, _ = schema_store(tmp_path)
    store.cache_root.write_text("not a directory")
    with pytest.raises(KubeconformSchemaStoreError, match="cannot write schema cache"):
        store.sync(lock)


def test_missing_upstream_version_is_a_configuration_error(tmp_path, monkeypatch):
    from chart_manager.commands.validate.schemas.errors import (
        KubeconformSchemaConfigurationError,
    )
    from chart_manager.integrations.kubeconform.repository_snapshot import (
        RepositorySnapshotDirectoryNotFoundError,
    )

    lock, store, snapshots = schema_store(tmp_path)

    def missing(*args, **kwargs):
        raise RepositorySnapshotDirectoryNotFoundError(
            "no schema directory v9.99.0-standalone-strict"
        )

    monkeypatch.setattr(snapshots, "checkout", missing)
    with pytest.raises(KubeconformSchemaConfigurationError, match="choose a Kubernetes version"):
        store.sync(lock)
    assert not list(store.root.rglob(".staging-*"))
