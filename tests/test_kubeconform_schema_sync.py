from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from chart_manager.integrations.kubeconform.github_schema_source import (
    GitHubKubeconformSchemaNotFoundError,
    GitHubKubeconformSchemaSourceEnvironmentError,
    GitHubKubeconformSchemaSourceIntegrityError,
)
from chart_manager.services.kubeconform_schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaLockError,
    KubeconformSchemaNotFoundError,
    KubeconformSchemaSourceEnvironmentError,
    KubeconformSchemaSourceIntegrityError,
)
from chart_manager.services.kubeconform_schemas.models import (
    AuthoredSchemaPolicy,
    GroupVersionKind,
    MaterializedSchema,
    SchemaRequirement,
    SchemaScope,
)
from chart_manager.services.kubeconform_schemas.source import (
    KubeconformSchemaArtifactBatch,
)
from chart_manager.services.kubeconform_schemas.store import KubeconformSchemaStore
from chart_manager.services.kubeconform_schemas.sync import (
    KubeconformSchemaSyncRequest,
    KubeconformSchemaSyncService,
)

_KUBE_SHA = "a" * 40
_CATALOG_SHA = "b" * 40
_DEPLOYMENT_SCHEMA = b'{"type":"object","title":"Deployment"}\n'
_WIDGET_SCHEMA = b'{"type":"object","title":"Widget"}\n'
_GENERATED_SCHEMA = b'{"type":"object","title":"Generated"}\n'


@dataclass(frozen=True)
class _Batch:
    content: dict[str, bytes]
    missing: tuple[str, ...]


class _Sources:
    def __init__(self) -> None:
        self.ref_calls: list[tuple[str, str]] = []
        self.download_calls: list[tuple[str, ...]] = []

    def resolve_ref(self, repository: str, ref: str) -> str:
        self.ref_calls.append((repository, ref))
        return _KUBE_SHA if repository.startswith("yannh/") else _CATALOG_SHA

    @staticmethod
    def artifact_url(repository: str, revision: str, path: str) -> str:
        return f"https://raw.githubusercontent.com/{repository}/{revision}/{path}"

    def fetch_many(
        self,
        requests,
        *,
        allow_not_found: bool = False,
    ) -> KubeconformSchemaArtifactBatch:
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
                from chart_manager.services.kubeconform_schemas.models import (
                    content_digest,
                )

                assert request.expected_sha256 == content_digest(value)
            content[request.key] = value
        return _Batch(
            content=content,
            missing=tuple(missing),
        )


class _FailingSources(_Sources):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def resolve_ref(self, repository: str, ref: str) -> str:
        raise self.error


def _request(
    tmp_path: Path,
    *,
    update: bool,
) -> KubeconformSchemaSyncRequest:
    scope = SchemaScope(chart="demo", environment="ci")
    generated_gvk = GroupVersionKind(group="generated.io", version="v1", kind="Generated")
    return KubeconformSchemaSyncRequest(
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
                scope=None,
                content=_GENERATED_SCHEMA,
                source_reference="rendered/crd.yaml#v1",
            ),
        ),
        lock_path=tmp_path / "repo/.chart-manager/schemas.lock.yaml",
        update=update,
    )


def test_update_resolves_refs_builds_complete_generation_and_writes_lock(
    tmp_path: Path,
) -> None:
    sources = _Sources()
    store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache")
    service = KubeconformSchemaSyncService(store, sources)
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


@pytest.mark.parametrize(
    ("adapter_error", "service_error"),
    [
        (
            GitHubKubeconformSchemaSourceEnvironmentError("network down"),
            KubeconformSchemaSourceEnvironmentError,
        ),
        (
            GitHubKubeconformSchemaSourceIntegrityError("invalid response"),
            KubeconformSchemaSourceIntegrityError,
        ),
        (
            GitHubKubeconformSchemaNotFoundError("missing object"),
            KubeconformSchemaNotFoundError,
        ),
    ],
)
def test_adapter_failures_are_translated_at_the_service_boundary(
    tmp_path: Path,
    adapter_error: Exception,
    service_error: type[Exception],
) -> None:
    store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache")
    service = KubeconformSchemaSyncService(
        store,
        _FailingSources(adapter_error),
    )

    with pytest.raises(service_error, match=str(adapter_error)):
        service.sync(_request(tmp_path, update=True))


def test_pinned_sync_is_cache_first_and_never_rewrites_lock(tmp_path: Path) -> None:
    initial_sources = _Sources()
    first_store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache-one")
    request = _request(tmp_path, update=True)
    created = KubeconformSchemaSyncService(first_store, initial_sources).sync(request)
    lock_bytes = request.lock_path.read_bytes()

    cached_sources = _Sources()
    cached = KubeconformSchemaSyncService(first_store, cached_sources).sync(
        KubeconformSchemaSyncRequest(**{**request.__dict__, "update": False})
    )

    assert cached.generation_path == created.generation_path
    assert not cached.lock_updated
    assert not cached.generation_published
    assert cached_sources.ref_calls == []
    assert cached_sources.download_calls == []
    assert request.lock_path.read_bytes() == lock_bytes

    # A separate cold machine hydrates immutable URLs from the same lock but
    # still has no authority to replace it.
    cold_sources = _Sources()
    cold_store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache-two")
    cold = KubeconformSchemaSyncService(cold_store, cold_sources).sync(
        KubeconformSchemaSyncRequest(**{**request.__dict__, "update": False})
    )
    assert cold_store.inspect(cold.lock).ready
    assert cold_sources.ref_calls == []
    assert cold_sources.download_calls
    assert request.lock_path.read_bytes() == lock_bytes


@pytest.mark.parametrize("cache_is_warm", [True, False])
def test_new_strict_gvk_is_stale_before_cache_or_source_access(
    tmp_path: Path,
    cache_is_warm: bool,
) -> None:
    initial_store = KubeconformSchemaStore("lab", cache_root=tmp_path / "warm")
    request = _request(tmp_path, update=True)
    KubeconformSchemaSyncService(initial_store, _Sources()).sync(request)
    sources = _Sources()
    store = initial_store if cache_is_warm else KubeconformSchemaStore(
        "lab", cache_root=tmp_path / "cold"
    )
    new_requirement = SchemaRequirement(
        gvk=GroupVersionKind(group="new.example.io", version="v1", kind="Gadget"),
        scope=SchemaScope(chart="consumer", environment="dev"),
    )

    with pytest.raises(KubeconformSchemaConfigurationError, match="--refresh"):
        KubeconformSchemaSyncService(store, sources).sync(
            KubeconformSchemaSyncRequest(
                **{
                    **request.__dict__,
                    "update": False,
                    "requirements": (*request.requirements, new_requirement),
                }
            )
        )

    assert sources.ref_calls == []
    assert sources.download_calls == []


@pytest.mark.parametrize("cache_is_warm", [True, False])
def test_changed_generated_bytes_are_stale_before_cache_or_source_access(
    tmp_path: Path,
    cache_is_warm: bool,
) -> None:
    initial_store = KubeconformSchemaStore("lab", cache_root=tmp_path / "warm")
    request = _request(tmp_path, update=True)
    KubeconformSchemaSyncService(initial_store, _Sources()).sync(request)
    generated = request.materialized[0]
    drifted = MaterializedSchema(
        gvk=generated.gvk,
        source="generated",
        scope=None,
        content=b'{"type":"object","title":"Changed"}\n',
        source_reference=generated.source_reference,
    )
    sources = _Sources()
    store = initial_store if cache_is_warm else KubeconformSchemaStore(
        "lab", cache_root=tmp_path / "cold"
    )

    with pytest.raises(KubeconformSchemaConfigurationError, match="changed generated"):
        KubeconformSchemaSyncService(store, sources).sync(
            KubeconformSchemaSyncRequest(
                **{**request.__dict__, "update": False, "materialized": (drifted,)}
            )
        )

    assert sources.download_calls == []


def test_generated_schema_taking_precedence_over_remote_requires_refresh(
    tmp_path: Path,
) -> None:
    store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache")
    request = _request(tmp_path, update=True)
    KubeconformSchemaSyncService(store, _Sources()).sync(request)
    widget = request.requirements[1].gvk
    generated_widget = MaterializedSchema(
        gvk=widget,
        source="generated",
        scope=None,
        content=b'{"type":"object","title":"Generated Widget"}\n',
        source_reference="rendered CRD example.io/v1/Widget",
    )

    with pytest.raises(KubeconformSchemaConfigurationError, match="schema lock is stale"):
        KubeconformSchemaSyncService(store, _Sources()).sync(
            KubeconformSchemaSyncRequest(
                **{
                    **request.__dict__,
                    "update": False,
                    "materialized": (*request.materialized, generated_widget),
                }
            )
        )


def test_allow_missing_new_gvk_does_not_change_compact_lock(tmp_path: Path) -> None:
    store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache")
    request = _request(tmp_path, update=True)
    created = KubeconformSchemaSyncService(store, _Sources()).sync(request)
    allowed = SchemaRequirement(
        gvk=GroupVersionKind(group="optional.example.io", version="v1", kind="Optional"),
        scope=SchemaScope(chart="consumer", environment="dev"),
        allow_missing=True,
    )
    sources = _Sources()

    result = KubeconformSchemaSyncService(store, sources).sync(
        KubeconformSchemaSyncRequest(
            **{
                **request.__dict__,
                "update": False,
                "requirements": (*request.requirements, allowed),
            }
        )
    )

    assert result.lock == created.lock
    assert sources.download_calls == []


def test_optional_upstream_schema_does_not_appear_only_after_refresh(tmp_path: Path) -> None:
    store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache")
    sources = _Sources()
    service = KubeconformSchemaSyncService(store, sources)
    request = _request(tmp_path, update=True)
    request = replace(request, requirements=tuple(
        item.model_copy(update={"allow_missing": True}) if item.gvk.kind == "Widget" else item
        for item in request.requirements
    ))
    initial = service.sync(request)
    assert not any(entry.gvk.kind == "Widget" for entry in initial.lock.schemas)
    assert not any("widget" in url for batch in sources.download_calls for url in batch)
    request = replace(request, update=False)
    assert service.sync(request).lock == initial.lock
    assert service.refresh(request).lock == initial.lock


def test_legacy_inventory_cache_does_not_block_new_format_sync(tmp_path: Path) -> None:
    store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache")
    service = KubeconformSchemaSyncService(store, _Sources())
    request = _request(tmp_path, update=True)
    initial = service.sync(request)
    legacy = store.cache_root / store.workspace / initial.generation_path.name
    legacy.parent.mkdir(parents=True)
    initial.generation_path.rename(legacy)
    (legacy / "inventory.json").write_text("{}")

    hydrated = service.sync(replace(request, update=False))
    assert store.inspect(hydrated.lock).ready
    assert hydrated.lock == initial.lock
    assert (legacy / "inventory.json").is_file()
    assert not (hydrated.generation_path / "inventory.json").exists()


@pytest.mark.parametrize("cache_is_warm", [True, False])
def test_changed_local_schema_bytes_are_stale_before_cache_or_source_access(
    tmp_path: Path,
    cache_is_warm: bool,
) -> None:
    scope = SchemaScope(chart="demo", environment="ci")
    local_gvk = GroupVersionKind(group="local.example.io", version="v1", kind="Local")
    base = _request(tmp_path, update=True)
    original = MaterializedSchema(
        gvk=local_gvk,
        source="local",
        scope=scope,
        content=b'{"type":"object","title":"Local"}\n',
        source_reference="charts/demo/schemas/local.json",
    )
    request = KubeconformSchemaSyncRequest(
        **{
            **base.__dict__,
            "requirements": (
                *base.requirements,
                SchemaRequirement(gvk=local_gvk, scope=scope),
            ),
            "materialized": (*base.materialized, original),
        }
    )
    warm = KubeconformSchemaStore("lab", cache_root=tmp_path / "warm")
    KubeconformSchemaSyncService(warm, _Sources()).sync(request)
    changed = MaterializedSchema(
        gvk=local_gvk,
        source="local",
        scope=scope,
        content=b'{"type":"object","title":"Changed Local"}\n',
        source_reference=original.source_reference,
    )
    sources = _Sources()
    store = warm if cache_is_warm else KubeconformSchemaStore(
        "lab", cache_root=tmp_path / "cold"
    )

    with pytest.raises(KubeconformSchemaConfigurationError, match="changed local"):
        KubeconformSchemaSyncService(store, sources).sync(
            KubeconformSchemaSyncRequest(
                **{
                    **request.__dict__,
                    "update": False,
                    "materialized": (*base.materialized, changed),
                }
            )
        )

    assert sources.download_calls == []


def test_unused_locked_schema_requires_refresh(tmp_path: Path) -> None:
    store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache")
    request = _request(tmp_path, update=True)
    KubeconformSchemaSyncService(store, _Sources()).sync(request)

    with pytest.raises(KubeconformSchemaConfigurationError, match="unused locked"):
        KubeconformSchemaSyncService(store, _Sources()).sync(
            KubeconformSchemaSyncRequest(
                **{
                    **request.__dict__,
                    "update": False,
                    "requirements": request.requirements[:-1],
                    "materialized": (),
                }
            )
        )


def test_lock_replace_failure_leaves_old_lock_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import chart_manager.services.kubeconform_schemas.sync as sync_module

    request = _request(tmp_path, update=True)
    request.lock_path.parent.mkdir(parents=True)
    request.lock_path.write_bytes(b"old-lock\n")
    store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache")

    def fail_write(_path, _lock) -> None:
        raise KubeconformSchemaLockError("simulated lock replace failure")

    monkeypatch.setattr(sync_module, "write_schema_lock_atomic", fail_write)

    with pytest.raises(KubeconformSchemaLockError, match="simulated"):
        KubeconformSchemaSyncService(store, _Sources()).sync(request)

    assert request.lock_path.read_bytes() == b"old-lock\n"
    generations = [path for path in store.root.iterdir() if not path.name.startswith(".")]
    assert len(generations) == 1, "a complete orphan generation is safe for later collection"


def test_derived_refresh_uses_existing_pins_without_resolving_refs(
    tmp_path: Path,
) -> None:
    store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache")
    request = _request(tmp_path, update=True)
    created = KubeconformSchemaSyncService(store, _Sources()).sync(request)
    shutil.rmtree(created.generation_path)
    sources = _Sources()
    drifted = KubeconformSchemaSyncRequest(
        **{
            **request.__dict__,
            "update": False,
            "requirements": request.requirements[:-1],
        }
    )

    result = KubeconformSchemaSyncService(store, sources).refresh(drifted)

    assert sources.ref_calls == []
    assert sources.download_calls
    assert result.lock.policy == created.lock.policy
    assert result.lock != created.lock


def test_new_use_of_repository_schema_does_not_change_lock(
    tmp_path: Path,
) -> None:
    store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache")
    request = _request(tmp_path, update=True)
    service = KubeconformSchemaSyncService(store, _Sources())
    created = service.sync(request)
    lock_bytes = request.lock_path.read_bytes()
    deployment = request.requirements[0]
    expanded = KubeconformSchemaSyncRequest(
        **{
            **request.__dict__,
            "update": False,
            "requirements": (
                *request.requirements,
                SchemaRequirement(
                    gvk=deployment.gvk,
                    scope=SchemaScope(chart="consumer", environment="dev"),
                ),
            ),
        }
    )

    # A second checkout may construct its own store object while sharing the
    # same XDG generation. Scope fan-out must not rewrite that generation.
    shared_store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache")
    before = {
        path.relative_to(created.generation_path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in created.generation_path.rglob("*")
        if path.is_file()
    }
    refreshed = KubeconformSchemaSyncService(shared_store, _Sources()).sync(expanded)
    after = {
        path.relative_to(created.generation_path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in created.generation_path.rglob("*")
        if path.is_file()
    }

    assert refreshed.lock == created.lock
    assert request.lock_path.read_bytes() == lock_bytes
    assert after == before


def test_materialized_drift_refreshes_lock_without_moving_pins(tmp_path: Path) -> None:
    store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache")
    request = _request(tmp_path, update=True)
    KubeconformSchemaSyncService(store, _Sources()).sync(request)
    lock_bytes = request.lock_path.read_bytes()
    sources = _Sources()
    generated = request.materialized[0]
    drifted = KubeconformSchemaSyncRequest(
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

    result = KubeconformSchemaSyncService(store, sources).refresh(drifted)

    assert sources.ref_calls == []
    assert result.lock_updated
    assert request.lock_path.read_bytes() != lock_bytes


def test_parallel_updates_serialize_without_nested_lock_deadlock(tmp_path: Path) -> None:
    store = KubeconformSchemaStore("lab", cache_root=tmp_path / "cache")
    service = KubeconformSchemaSyncService(store, _Sources())
    request = _request(tmp_path, update=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.sync, request) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]

    assert results[0].lock == results[1].lock
    assert store.inspect(results[0].lock).ready
