from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.services.kubeconform_schemas.errors import KubeconformSchemaLockError
from chart_manager.services.kubeconform_schemas.lock import (
    load_schema_lock,
    serialize_schema_lock,
    write_schema_lock_atomic,
)
from chart_manager.services.kubeconform_schemas.models import (
    GroupVersionKind,
    LockedSchemaPolicy,
    RepositoryPin,
    SchemaFile,
    SchemaRequirement,
    SchemaScope,
    build_lock,
    content_digest,
)


def _policy() -> LockedSchemaPolicy:
    return LockedSchemaPolicy(
        kubernetes_version="1.35.3",
        generate_from_crds=True,
        kubernetes=RepositoryPin(
            repository="yannh/kubernetes-json-schema",
            track="master",
            resolved="a" * 40,
        ),
        catalog=RepositoryPin(
            repository="datreeio/CRDs-catalog",
            track="main",
            resolved="b" * 40,
        ),
    )


def _lock():
    content = b'{"type":"object"}\n'
    gvk = GroupVersionKind(group="apps", version="v1", kind="Deployment")
    scope = SchemaScope(chart="demo", environment="ci")
    return build_lock(
        workspace="lab-charts",
        policy=_policy(),
        inventory=[SchemaRequirement(gvk=gvk, scope=scope)],
        schemas=[
            SchemaFile(
                gvk=gvk,
                source="kubernetes",
                path="kubernetes/apps/deployment_v1.json",
                sha256=content_digest(content),
                source_reference="https://example.invalid/schema.json",
            )
        ],
    )


def test_lock_serialization_is_stable_and_round_trips(tmp_path: Path) -> None:
    lock = _lock()
    path = tmp_path / "schemas.lock.yaml"

    first = serialize_schema_lock(lock)
    write_schema_lock_atomic(path, lock)

    assert path.read_text() == first
    assert load_schema_lock(path) == lock
    assert serialize_schema_lock(load_schema_lock(path)) == first
    assert "version: 1\n" in first
    assert "workspace: lab-charts\n" in first
    assert "generation: sha256:" in first


def test_lock_rejects_a_generation_digest_that_does_not_describe_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schemas.lock.yaml"
    text = serialize_schema_lock(_lock()).replace("generation: sha256:", "generation: sha256:0")
    path.write_text(text)

    with pytest.raises(KubeconformSchemaLockError, match="generation"):
        load_schema_lock(path)


def test_lock_wraps_malformed_yaml_as_a_typed_failure(tmp_path: Path) -> None:
    path = tmp_path / "schemas.lock.yaml"
    path.write_text("inventory: [\n")

    with pytest.raises(KubeconformSchemaLockError, match="invalid schema lock"):
        load_schema_lock(path)


def test_build_lock_omits_scope_inventory_and_sorts_schema_entries() -> None:
    scope = SchemaScope(chart="demo", environment="ci")
    config_map = GroupVersionKind(version="v1", kind="ConfigMap")
    deployment = GroupVersionKind(group="apps", version="v1", kind="Deployment")
    content = b"{}"

    lock = build_lock(
        workspace="lab-charts",
        policy=_policy(),
        inventory=[
            SchemaRequirement(gvk=deployment, scope=scope),
            SchemaRequirement(gvk=config_map, scope=scope),
        ],
        schemas=[
            SchemaFile(
                gvk=deployment,
                source="kubernetes",
                path="kubernetes/apps/deployment_v1.json",
                sha256=content_digest(content),
                source_reference="d",
            ),
            SchemaFile(
                gvk=config_map,
                source="kubernetes",
                path="kubernetes/configmap_v1.json",
                sha256=content_digest(content),
                source_reference="c",
            ),
        ],
    )

    assert lock.inventory == ()
    assert "inventory:" not in serialize_schema_lock(lock)
    assert [item.gvk.kind for item in lock.schemas] == ["ConfigMap", "Deployment"]
