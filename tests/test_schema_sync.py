from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chart_manager.integrations.schema_sources import DownloadBatch
from chart_manager.services.schemas.errors import (
    SchemaIntegrityError,
    SchemaLockError,
    SchemaSourceEnvironmentError,
)
from chart_manager.services.schemas.models import (
    AuthoredSchemaPolicy,
    GroupVersionKind,
    MaterializedSchema,
    SchemaRequirement,
    SchemaScope,
)
from chart_manager.services.schemas.store import SchemaStore
from chart_manager.services.schemas.sync import (
    SchemaSyncRequest,
    SchemaSyncService,
)

_KUBE_SHA = "a" * 40
_CATALOG_SHA = "b" * 40
_DEPLOYMENT_SCHEMA = b'{"type":"object","title":"Deployment"}\n'
_WIDGET_SCHEMA = b'{"type":"object","title":"Widget"}\n'
_GENERATED_SCHEMA = b'{"type":"object","title":"Generated"}\n'


class _Sources:
    def __init__(self) -> None:
        self.ref_calls: list[tuple[str, str]] = []
        self.download_calls: list[tuple[str, ...]] = []

    def resolve_github_ref(self, repository: str, ref: str) -> str:
        self.ref_calls.append((repository, ref))
        return _KUBE_SHA if repository.startswith("yannh/") else _CATALOG_SHA

    def download_many(self, requests, *, allow_not_found: bool = False) -> DownloadBatch:
        self.download_calls.append(tuple(request.url for request in requests))
        content: dict[str, bytes] = {}
        missing: list[str] = []
        for request in requests:
            if "deployment-apps-v1.json" in request.url:
                value = _DEPLOYMENT_SCHEMA
            elif "/example.io/widget_v1.json" in request.url:
                value = _WIDGET_SCHEMA
            elif "widget-example-v1.json" in request.url:
                if not allow_not_found:
                    raise AssertionError("in-tree Widget 404 must permit catalog fallback")
                missing.append(request.key)
                continue
            else:
                raise AssertionError(f"unexpected download: {request.url}")
            if request.expected_sha256 is not None:
                from chart_manager.services.schemas.models import content_digest

                assert request.expected_sha256 == content_digest(value)
            content[request.key] = value
        return DownloadBatch(content=content, missing=tuple(missing))


def _request(tmp_path: Path, *, update: bool, offline: bool = False) -> SchemaSyncRequest:
    scope = SchemaScope(chart="demo", environment="ci")
    generated_gvk = GroupVersionKind(group="generated.io", version="v1", kind="Generated")
    return SchemaSyncRequest(
        workspace="lab",
        policy=AuthoredSchemaPolicy(
            kubernetes_version="1.35.3",
            generate_from_crds=True,
            catalog_repository="datreeio/CRDs-catalog",
            catalog_track="main",
        ),
        requirements=(
            SchemaRequirement(
                gvk=GroupVersionKind(group="apps", version="v1", kind="Deployment"),
                scope=scope,
            ),
            SchemaRequirement(
                gvk=GroupVersionKind(group="example.io", version="v1", kind="Widget"),
                scope=scope,
            ),
            SchemaRequirement(gvk=generated_gvk, scope=scope),
        ),
        materialized=(
            MaterializedSchema(
                gvk=generated_gvk,
                source="generated",
                scope=scope,
                content=_GENERATED_SCHEMA,
                source_reference="rendered/crd.yaml#v1",
            ),
        ),
        lock_path=tmp_path / "repo/.chart-manager/schemas.lock.yaml",
        update=update,
        offline=offline,
    )


def test_update_resolves_refs_builds_complete_generation_and_writes_lock(
    tmp_path: Path,
) -> None:
    sources = _Sources()
    store = SchemaStore("lab", cache_root=tmp_path / "cache")
    service = SchemaSyncService(store, sources)  # type: ignore[arg-type]
    request = _request(tmp_path, update=True)

    result = service.sync(request)

    assert result.lock_updated
    assert result.generation_published
    assert result.generation_path.is_dir()
    assert request.lock_path.is_file()
    assert store.inspect(result.lock).ready
    assert [entry.source for entry in result.lock.schemas] == [
        "kubernetes",
        "catalog",
        "generated",
    ]
    assert sources.ref_calls == [
        ("yannh/kubernetes-json-schema", "master"),
        ("datreeio/CRDs-catalog", "main"),
    ]
    locations = result.locations_for(SchemaScope(chart="demo", environment="ci"))
    assert locations.generated_schema_locations
    assert locations.fallback_schema_locations


def test_pinned_sync_is_cache_first_and_never_rewrites_lock(tmp_path: Path) -> None:
    initial_sources = _Sources()
    first_store = SchemaStore("lab", cache_root=tmp_path / "cache-one")
    request = _request(tmp_path, update=True)
    created = SchemaSyncService(first_store, initial_sources).sync(request)  # type: ignore[arg-type]
    lock_bytes = request.lock_path.read_bytes()

    cached_sources = _Sources()
    cached = SchemaSyncService(first_store, cached_sources).sync(
        SchemaSyncRequest(**{**request.__dict__, "update": False})
    )  # type: ignore[arg-type]

    assert cached.generation_path == created.generation_path
    assert not cached.lock_updated
    assert not cached.generation_published
    assert cached_sources.ref_calls == []
    assert cached_sources.download_calls == []
    assert request.lock_path.read_bytes() == lock_bytes

    # A separate cold machine hydrates immutable URLs from the same lock but
    # still has no authority to replace it.
    cold_sources = _Sources()
    cold_store = SchemaStore("lab", cache_root=tmp_path / "cache-two")
    cold = SchemaSyncService(cold_store, cold_sources).sync(
        SchemaSyncRequest(**{**request.__dict__, "update": False})
    )  # type: ignore[arg-type]
    assert cold_store.inspect(cold.lock).ready
    assert cold_sources.ref_calls == []
    assert request.lock_path.read_bytes() == lock_bytes


def test_offline_cold_sync_reports_environment_and_does_not_mutate(tmp_path: Path) -> None:
    warm_store = SchemaStore("lab", cache_root=tmp_path / "warm")
    request = _request(tmp_path, update=True)
    SchemaSyncService(warm_store, _Sources()).sync(request)  # type: ignore[arg-type]
    lock_bytes = request.lock_path.read_bytes()
    cold_store = SchemaStore("lab", cache_root=tmp_path / "cold")

    with pytest.raises(SchemaSourceEnvironmentError, match="not available offline"):
        SchemaSyncService(cold_store, _Sources()).sync(
            SchemaSyncRequest(**{**request.__dict__, "update": False, "offline": True})
        )  # type: ignore[arg-type]

    assert request.lock_path.read_bytes() == lock_bytes
    assert not any(cold_store.root.glob("[!.]*"))


def test_lock_replace_failure_leaves_old_lock_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import chart_manager.services.schemas.sync as sync_module

    request = _request(tmp_path, update=True)
    request.lock_path.parent.mkdir(parents=True)
    request.lock_path.write_bytes(b"old-lock\n")
    store = SchemaStore("lab", cache_root=tmp_path / "cache")

    def fail_write(_path, _lock) -> None:
        raise SchemaLockError("simulated lock replace failure")

    monkeypatch.setattr(sync_module, "write_schema_lock_atomic", fail_write)

    with pytest.raises(SchemaLockError, match="simulated"):
        SchemaSyncService(store, _Sources()).sync(request)  # type: ignore[arg-type]

    assert request.lock_path.read_bytes() == b"old-lock\n"
    generations = [path for path in store.root.iterdir() if not path.name.startswith(".")]
    assert len(generations) == 1, "a complete orphan generation is safe for later collection"


def test_inventory_drift_blocks_pinned_sync_before_network_or_store_mutation(
    tmp_path: Path,
) -> None:
    store = SchemaStore("lab", cache_root=tmp_path / "cache")
    request = _request(tmp_path, update=True)
    created = SchemaSyncService(store, _Sources()).sync(request)  # type: ignore[arg-type]
    shutil.rmtree(created.generation_path)
    sources = _Sources()
    drifted = SchemaSyncRequest(
        **{
            **request.__dict__,
            "update": False,
            "requirements": request.requirements[:-1],
        }
    )

    with pytest.raises(SchemaLockError, match="inventory differs"):
        SchemaSyncService(store, sources).sync(drifted)  # type: ignore[arg-type]

    assert sources.ref_calls == []
    assert sources.download_calls == []


def test_materialized_drift_blocks_warm_pinned_sync_without_mutation(tmp_path: Path) -> None:
    store = SchemaStore("lab", cache_root=tmp_path / "cache")
    request = _request(tmp_path, update=True)
    SchemaSyncService(store, _Sources()).sync(request)  # type: ignore[arg-type]
    lock_bytes = request.lock_path.read_bytes()
    sources = _Sources()
    generated = request.materialized[0]
    drifted = SchemaSyncRequest(
        **{
            **request.__dict__,
            "update": False,
            "materialized": (
                MaterializedSchema(
                    gvk=generated.gvk,
                    source=generated.source,
                    scope=generated.scope,
                    content=b'{"type":"object","title":"Changed"}\n',
                    source_reference=generated.source_reference,
                ),
            ),
        }
    )

    with pytest.raises(SchemaIntegrityError, match="materialized schema drift"):
        SchemaSyncService(store, sources).sync(drifted)  # type: ignore[arg-type]

    assert sources.ref_calls == []
    assert sources.download_calls == []
    assert request.lock_path.read_bytes() == lock_bytes


def test_parallel_updates_serialize_without_nested_lock_deadlock(tmp_path: Path) -> None:
    store = SchemaStore("lab", cache_root=tmp_path / "cache")
    service = SchemaSyncService(store, _Sources())  # type: ignore[arg-type]
    request = _request(tmp_path, update=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.sync, request) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]

    assert results[0].lock == results[1].lock
    assert store.inspect(results[0].lock).ready
