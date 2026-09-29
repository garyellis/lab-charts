"""Read-only schema policy, lock, and store diagnostics."""

from __future__ import annotations

from pathlib import Path

from chart_manager.api.v1alpha1.chart_workspace import WorkspaceValidation
from chart_manager.domain.workspace import SCHEMA_LOCK_FILE, RepositoryWorkspace
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import CheckStatus
from chart_manager.services.kubeconform_schemas.doctor import KubeconformSchemaDoctor
from chart_manager.services.kubeconform_schemas.lock import write_schema_lock_atomic
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
from chart_manager.services.kubeconform_schemas.store import KubeconformSchemaStore

_CONTENT = b'{"type":"object"}\n'


def _workspace(root: Path, *, version: str = "1.35.3") -> RepositoryWorkspace:
    validation = WorkspaceValidation.model_validate(
        {
            "kubernetesVersion": version,
            "schemas": {
                "generateFromCRDs": True,
                "catalog": {
                    "repository": "datreeio/CRDs-catalog",
                    "track": "main",
                },
            },
        }
    )
    return RepositoryWorkspace(
        root=root,
        name="lab-charts",
        validation=validation,
        authored=True,
    )


def _lock(*, version: str = "1.35.3", with_schema: bool = True):
    gvk = GroupVersionKind(group="apps", version="v1", kind="Deployment")
    scope = SchemaScope(chart="demo", environment="dev")
    schemas = (
        [
            SchemaFile(
                gvk=gvk,
                source="kubernetes",
                path="kubernetes/apps/deployment_v1.json",
                sha256=content_digest(_CONTENT),
                source_reference="https://example.invalid/deployment.json",
            )
        ]
        if with_schema
        else []
    )
    return build_lock(
        workspace="lab-charts",
        policy=LockedSchemaPolicy(
            kubernetes_version=version,
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
        ),
        inventory=[SchemaRequirement(gvk=gvk, scope=scope)],
        schemas=schemas,
    )


def _write_lock(root: Path, lock) -> Path:
    path = root / SCHEMA_LOCK_FILE
    write_schema_lock_atomic(path, lock)
    return path


def _by_name(doctor: KubeconformSchemaDoctor):
    return {check.name: check for check in doctor.preflight()}


def test_ready_generation_reports_policy_lock_coverage_and_offline_readiness(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    lock = _lock()
    lock_path = _write_lock(tmp_path, lock)
    cache_root = tmp_path / "cache"
    generation = KubeconformSchemaStore(
        "lab-charts",
        cache_root=cache_root,
    ).generation_path(lock)
    schema = generation / "kubernetes/apps/deployment_v1.json"
    schema.parent.mkdir(parents=True)
    schema.write_bytes(_CONTENT)
    before_lock = lock_path.read_bytes()
    before_schema = schema.read_bytes()

    checks = _by_name(KubeconformSchemaDoctor(workspace, cache_root=cache_root))

    assert checks["schema-policy"].status is CheckStatus.OK
    assert checks["schema-policy"].data == {
        "configured": True,
        "workspace": "lab-charts",
        "kubernetesVersion": "1.35.3",
        "generateFromCRDs": True,
        "kubernetes": {
            "repository": "yannh/kubernetes-json-schema",
            "track": "master",
        },
        "catalog": {
            "repository": "datreeio/CRDs-catalog",
            "track": "main",
        },
    }
    assert checks["schema-lock"].status is CheckStatus.OK
    assert checks["schema-lock"].data["matchesPolicy"] is True
    store = checks["schema-store"]
    assert store.status is CheckStatus.OK
    assert store.data["path"] == str(generation)
    assert store.data["generation"] == lock.generation
    assert store.data["expected"] == 1
    assert store.data["present"] == 1
    assert store.data["missing"] == 0
    assert store.data["corrupt"] == 0
    assert store.data["uncovered"] == 0
    assert store.data["ready"] is True
    assert store.data["offlineReady"] is True
    assert store.data["missingGVKs"] == []
    assert store.data["sourceCoverage"]["kubernetes"] == {
        "files": 1,
        "requirements": 1,
    }
    assert lock_path.read_bytes() == before_lock
    assert schema.read_bytes() == before_schema


def test_missing_generation_is_environmental_and_names_missing_gvk(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    lock = _lock()
    _write_lock(tmp_path, lock)
    cache_root = tmp_path / "cache"

    check = _by_name(KubeconformSchemaDoctor(workspace, cache_root=cache_root))["schema-store"]

    assert check.status is CheckStatus.FAILED
    assert check.outcome is Outcome.ENVIRONMENT
    assert check.remediation == "run chart-manager schemas sync while online"
    assert check.data["missing"] == 1
    assert check.data["offlineReady"] is False
    assert check.data["missingGVKs"] == [{"scope": "demo/dev", "gvk": "apps/v1/Deployment"}]
    assert not cache_root.exists(), "doctor must not create the missing store"


def test_corrupt_generation_is_a_tool_failure_with_repair_command(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    lock = _lock()
    _write_lock(tmp_path, lock)
    cache_root = tmp_path / "cache"
    generation = KubeconformSchemaStore(
        "lab-charts",
        cache_root=cache_root,
    ).generation_path(lock)
    schema = generation / "kubernetes/apps/deployment_v1.json"
    schema.parent.mkdir(parents=True)
    schema.write_bytes(b"{}")

    check = _by_name(KubeconformSchemaDoctor(workspace, cache_root=cache_root))["schema-store"]

    assert check.status is CheckStatus.FAILED
    assert check.outcome is Outcome.TOOL
    assert check.data["corrupt"] == 1
    assert check.data["offlineReady"] is False
    assert check.remediation == (
        f"remove {generation}, then run chart-manager schemas sync while online"
    )
    assert schema.read_bytes() == b"{}", "doctor must not repair corrupt content"


def test_uncovered_inventory_is_a_spec_failure_with_update_remediation(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    lock = _lock(with_schema=False)
    _write_lock(tmp_path, lock)

    check = _by_name(KubeconformSchemaDoctor(workspace, cache_root=tmp_path / "cache"))[
        "schema-store"
    ]

    assert check.status is CheckStatus.FAILED
    assert check.outcome is Outcome.SPEC
    assert check.data["uncovered"] == 1
    assert check.data["missingGVKs"] == [{"scope": "demo/dev", "gvk": "apps/v1/Deployment"}]
    assert "schemas sync --update" in (check.remediation or "")
    assert "ignoreMissingSchemas" in (check.remediation or "")


def test_lock_policy_mismatch_stops_before_store_and_prescribes_update(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, version="1.35.3")
    _write_lock(tmp_path, _lock(version="1.34.0"))
    cache_root = tmp_path / "cache"

    checks = _by_name(KubeconformSchemaDoctor(workspace, cache_root=cache_root))

    lock = checks["schema-lock"]
    assert lock.status is CheckStatus.FAILED
    assert lock.outcome is Outcome.SPEC
    assert lock.remediation == "run chart-manager schemas sync --update"
    assert lock.data["matchesPolicy"] is False
    assert "kubernetesVersion" in lock.data["mismatches"][0]
    store = checks["schema-store"]
    assert store.status is CheckStatus.FAILED
    assert store.data["generation"] == lock.data["generation"]
    assert store.data["offlineReady"] is False
    assert not cache_root.exists()


def test_malformed_lock_is_reported_without_mutation(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    lock_path = tmp_path / SCHEMA_LOCK_FILE
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("not: [valid\n", encoding="utf-8")
    before = lock_path.read_bytes()

    checks = _by_name(KubeconformSchemaDoctor(workspace, cache_root=tmp_path / "cache"))

    assert checks["schema-lock"].status is CheckStatus.FAILED
    assert checks["schema-lock"].outcome is Outcome.SPEC
    assert checks["schema-lock"].remediation == ("run chart-manager schemas sync --update")
    assert checks["schema-store"].status is CheckStatus.SKIPPED
    assert lock_path.read_bytes() == before
