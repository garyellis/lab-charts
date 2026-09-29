from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.services.schemas.errors import SchemaStoreError
from chart_manager.services.schemas.models import (
    GroupVersionKind,
    LockedSchemaPolicy,
    RepositoryPin,
    SchemaFile,
    SchemaRequirement,
    SchemaScope,
    build_lock,
    content_digest,
)
from chart_manager.services.schemas.store import (
    SchemaStore,
    artifact_relative_path,
    kubeconform_schema_locations,
)


def _policy() -> LockedSchemaPolicy:
    return LockedSchemaPolicy(
        kubernetes_version="1.35.3",
        generate_from_crds=True,
        kubernetes=RepositoryPin(
            repository="yannh/kubernetes-json-schema", track="master", resolved="a" * 40
        ),
        catalog=RepositoryPin(
            repository="datreeio/CRDs-catalog", track="main", resolved="b" * 40
        ),
    )


def test_store_publishes_verified_immutable_generation(tmp_path: Path) -> None:
    content = b'{"type":"object"}\n'
    gvk = GroupVersionKind(group="apps", version="v1", kind="Deployment")
    scope = SchemaScope(chart="demo", environment="ci")
    entry = SchemaFile(
        gvk=gvk,
        source="kubernetes",
        path=artifact_relative_path(source="kubernetes", gvk=gvk, scope=None),
        sha256=content_digest(content),
        source_reference="https://example/schema",
    )
    lock = build_lock(
        workspace="lab",
        policy=_policy(),
        inventory=[SchemaRequirement(gvk=gvk, scope=scope)],
        schemas=[entry],
    )
    store = SchemaStore("lab", cache_root=tmp_path / "cache")
    stage = store.create_stage()
    store.write_stage_file(stage, entry, content)

    published = store.publish_generation(stage, lock)

    assert published == store.generation_path(lock)
    assert store.inspect(lock).ready
    assert not stage.exists()

    (published / entry.path).write_text("{}")
    with pytest.raises(SchemaStoreError, match="checksum"):
        store.require_ready(lock)


def test_store_rejects_files_not_declared_by_the_lock(tmp_path: Path) -> None:
    content = b"{}"
    gvk = GroupVersionKind(version="v1", kind="ConfigMap")
    scope = SchemaScope(chart="demo", environment="ci")
    entry = SchemaFile(
        gvk=gvk,
        source="kubernetes",
        path="kubernetes/configmap_v1.json",
        sha256=content_digest(content),
        source_reference="https://example/schema",
    )
    lock = build_lock(
        workspace="lab",
        policy=_policy(),
        inventory=[SchemaRequirement(gvk=gvk, scope=scope)],
        schemas=[entry],
    )
    store = SchemaStore("lab", cache_root=tmp_path / "cache")
    stage = store.create_stage()
    store.write_stage_file(stage, entry, content)
    unexpected = stage / "kubernetes/unlocked.json"
    unexpected.write_text("{}")

    with pytest.raises(SchemaStoreError, match="not declared by the lock"):
        store.publish_generation(stage, lock)
    assert not store.generation_path(lock).exists()


def test_store_rejects_incomplete_generation_before_rename(tmp_path: Path) -> None:
    gvk = GroupVersionKind(version="v1", kind="ConfigMap")
    scope = SchemaScope(chart="demo", environment="ci")
    entry = SchemaFile(
        gvk=gvk,
        source="kubernetes",
        path="kubernetes/configmap_v1.json",
        sha256=content_digest(b"{}"),
        source_reference="https://example/schema",
    )
    lock = build_lock(
        workspace="lab",
        policy=_policy(),
        inventory=[SchemaRequirement(gvk=gvk, scope=scope)],
        schemas=[entry],
    )
    store = SchemaStore("lab", cache_root=tmp_path / "cache")
    stage = store.create_stage()

    with pytest.raises(SchemaStoreError, match="missing"):
        store.publish_generation(stage, lock)
    assert not store.generation_path(lock).exists()


def test_kubeconform_locations_split_generated_from_fallbacks(tmp_path: Path) -> None:
    content = b"{}"
    gvk = GroupVersionKind(group="example.io", version="v1", kind="Widget")
    scope = SchemaScope(chart="demo", environment="ci")
    entries = [
        SchemaFile(
            gvk=gvk,
            source="generated",
            scope=scope,
            path=artifact_relative_path(source="generated", gvk=gvk, scope=scope),
            sha256=content_digest(content),
            source_reference="crd.yaml",
        ),
        SchemaFile(
            gvk=GroupVersionKind(version="v1", kind="ConfigMap"),
            source="kubernetes",
            path="kubernetes/configmap_v1.json",
            sha256=content_digest(content),
            source_reference="https://example/configmap",
        ),
    ]
    lock = build_lock(
        workspace="lab",
        policy=_policy(),
        inventory=[SchemaRequirement(gvk=gvk, scope=scope)],
        schemas=entries,
    )

    locations = kubeconform_schema_locations(lock, tmp_path / "generation", scope=scope)

    assert len(locations.generated_schema_locations) == 1
    assert "/generated/demo/ci/" in locations.generated_schema_locations[0]
    assert len(locations.fallback_schema_locations) == 1
    assert "/kubernetes/" in locations.fallback_schema_locations[0]
