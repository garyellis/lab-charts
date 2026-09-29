"""Eager schema synchronization against either tracking refs or an existing lock."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from chart_manager.integrations.schema_sources import (
    SchemaDownload,
    SchemaSourceClient,
    raw_github_url,
)
from chart_manager.services.schemas.errors import (
    SchemaConfigurationError,
    SchemaIntegrityError,
    SchemaLockError,
    SchemaSourceEnvironmentError,
)
from chart_manager.services.schemas.lock import (
    load_schema_lock,
    write_schema_lock_atomic,
)
from chart_manager.services.schemas.models import (
    AuthoredSchemaPolicy,
    GroupVersionKind,
    LockedSchemaPolicy,
    MaterializedSchema,
    RepositoryPin,
    SchemaFile,
    SchemaLock,
    SchemaRequirement,
    SchemaScope,
    build_lock,
    content_digest,
    sort_requirements,
)
from chart_manager.services.schemas.store import (
    KubeconformSchemaLocations,
    SchemaStore,
    artifact_relative_path,
    kubeconform_schema_locations,
)


@dataclass(frozen=True)
class SchemaSyncRequest:
    workspace: str
    policy: AuthoredSchemaPolicy
    requirements: tuple[SchemaRequirement, ...]
    materialized: tuple[MaterializedSchema, ...]
    lock_path: Path
    update: bool = False
    offline: bool = False


@dataclass(frozen=True)
class SchemaSyncResult:
    lock: SchemaLock
    generation_path: Path
    lock_updated: bool
    generation_published: bool

    def locations_for(self, scope: SchemaScope) -> KubeconformSchemaLocations:
        """Resolve local-only kubeconform locations for one chart/environment."""
        return kubeconform_schema_locations(
            self.lock,
            self.generation_path,
            scope=scope,
        )


class SchemaSyncService:
    """Build complete immutable generations without exposing partial state."""

    def __init__(self, store: SchemaStore, source_client: SchemaSourceClient) -> None:
        self.store = store
        self.source_client = source_client

    def sync(self, request: SchemaSyncRequest) -> SchemaSyncResult:
        """Return a ready generation, using a valid cache before any source access.

        With ``update=False`` the committed lock is read-only: even a cold-cache
        hydration publishes only the lock's immutable generation. Only an explicit
        update resolves tracking refs and replaces the repository lock.
        """
        if request.workspace != self.store.workspace:
            raise SchemaConfigurationError(
                f"sync workspace {request.workspace!r} does not match store "
                f"workspace {self.store.workspace!r}"
            )
        requirements = sort_requirements(list(request.requirements))
        with self.store.serialized_sync():
            if request.update:
                if request.offline:
                    raise SchemaSourceEnvironmentError(
                        "schemas sync --update cannot resolve tracking refs offline"
                    )
                lock, content = self._build_updated_lock(request, requirements)
                return self._publish(
                    request,
                    lock,
                    content,
                    replace_lock=True,
                )
            lock = self._load_existing(request, requirements)
            status = self.store.inspect(lock)
            if status.ready:
                return SchemaSyncResult(
                    lock=lock,
                    generation_path=status.generation_path,
                    lock_updated=False,
                    generation_published=False,
                )
            if request.offline:
                raise SchemaSourceEnvironmentError(
                    f"schema generation {lock.generation} is not available offline; "
                    "run `chart-manager schemas sync` while online"
                )
            content = self._hydrate_locked_content(lock, request.materialized)
            return self._publish(
                request,
                lock,
                content,
                replace_lock=False,
            )

    def _build_updated_lock(
        self,
        request: SchemaSyncRequest,
        requirements: tuple[SchemaRequirement, ...],
    ) -> tuple[SchemaLock, dict[str, bytes]]:
        policy = request.policy
        kubernetes_resolved = self.source_client.resolve_github_ref(
            policy.kubernetes_repository, policy.kubernetes_track
        )
        catalog_resolved = self.source_client.resolve_github_ref(
            policy.catalog_repository, policy.catalog_track
        )
        locked_policy = LockedSchemaPolicy(
            kubernetes_version=policy.normalized_version(),
            generate_from_crds=policy.generate_from_crds,
            kubernetes=RepositoryPin(
                repository=policy.kubernetes_repository,
                track=policy.kubernetes_track,
                resolved=kubernetes_resolved,
            ),
            catalog=RepositoryPin(
                repository=policy.catalog_repository,
                track=policy.catalog_track,
                resolved=catalog_resolved,
            ),
        )

        materialized = _index_materialized(request.materialized)
        selected: dict[tuple[str, str, str, str, str, str], MaterializedSchema] = {}
        unresolved: list[SchemaRequirement] = []
        for requirement in requirements:
            artifact = _select_materialized(materialized, requirement)
            if artifact is None:
                unresolved.append(requirement)
                continue
            selected[_artifact_key(artifact)] = artifact

        remote_by_gvk: dict[
            tuple[str, str, str],
            tuple[Literal["kubernetes", "catalog"], bytes, str],
        ] = {}
        unresolved_gvks = _unique_gvks(unresolved)
        kubernetes_requests = [
            SchemaDownload(
                key=gvk.key,
                url=_kubernetes_schema_url(locked_policy, gvk),
            )
            for gvk in unresolved_gvks
        ]
        kubernetes_batch = self.source_client.download_many(
            kubernetes_requests,
            allow_not_found=True,
        )
        for gvk in unresolved_gvks:
            content = kubernetes_batch.content.get(gvk.key)
            if content is not None:
                _validate_schema_json(content, source=_kubernetes_schema_url(locked_policy, gvk))
                remote_by_gvk[_gvk_key(gvk)] = (
                    "kubernetes",
                    content,
                    _kubernetes_schema_url(locked_policy, gvk),
                )

        kubernetes_missing = set(kubernetes_batch.missing)
        catalog_gvks = [
            gvk for gvk in unresolved_gvks if gvk.key in kubernetes_missing and gvk.group
        ]
        catalog_requests = [
            SchemaDownload(key=gvk.key, url=_catalog_schema_url(locked_policy, gvk))
            for gvk in catalog_gvks
        ]
        catalog_batch = self.source_client.download_many(
            catalog_requests,
            allow_not_found=True,
        )
        for gvk in catalog_gvks:
            content = catalog_batch.content.get(gvk.key)
            if content is not None:
                _validate_schema_json(content, source=_catalog_schema_url(locked_policy, gvk))
                remote_by_gvk[_gvk_key(gvk)] = (
                    "catalog",
                    content,
                    _catalog_schema_url(locked_policy, gvk),
                )

        missing = [
            requirement
            for requirement in unresolved
            if _gvk_key(requirement.gvk) not in remote_by_gvk and not requirement.allow_missing
        ]
        if missing:
            detail = ", ".join(
                sorted(f"{item.scope.key}: {item.gvk.key}" for item in missing)
            )
            raise SchemaIntegrityError(
                "required schemas were not found in generated, local, Kubernetes, "
                f"or catalog sources: {detail}"
            )

        files: list[SchemaFile] = []
        content_by_path: dict[str, bytes] = {}
        for artifact in selected.values():
            _validate_schema_json(artifact.content, source=artifact.source_reference)
            path = artifact_relative_path(
                source=artifact.source,
                gvk=artifact.gvk,
                scope=artifact.scope,
            )
            entry = SchemaFile(
                gvk=artifact.gvk,
                source=artifact.source,
                scope=artifact.scope,
                path=path,
                sha256=artifact.sha256,
                source_reference=artifact.source_reference,
            )
            files.append(entry)
            _put_content(content_by_path, entry.path, artifact.content)

        for gvk_key, (source, content, url) in remote_by_gvk.items():
            gvk = next(gvk for gvk in unresolved_gvks if _gvk_key(gvk) == gvk_key)
            path = artifact_relative_path(source=source, gvk=gvk, scope=None)
            entry = SchemaFile(
                gvk=gvk,
                source=source,
                scope=None,
                path=path,
                sha256=content_digest(content),
                source_reference=url,
            )
            files.append(entry)
            _put_content(content_by_path, entry.path, content)

        lock = build_lock(
            workspace=request.workspace,
            policy=locked_policy,
            inventory=list(requirements),
            schemas=files,
        )
        return lock, content_by_path

    def _load_existing(
        self,
        request: SchemaSyncRequest,
        requirements: tuple[SchemaRequirement, ...],
    ) -> SchemaLock:
        if not request.lock_path.is_file():
            raise SchemaLockError(
                f"schema lock does not exist: {request.lock_path}; "
                "run `chart-manager schemas sync --update`"
            )
        lock = load_schema_lock(request.lock_path)
        if lock.workspace != request.workspace:
            raise SchemaLockError(
                f"schema lock is for workspace {lock.workspace!r}, not {request.workspace!r}"
            )
        expected_policy = request.policy
        actual = lock.policy
        mismatches: list[str] = []
        comparisons = (
            ("kubernetesVersion", actual.kubernetes_version, expected_policy.normalized_version()),
            ("generateFromCRDs", actual.generate_from_crds, expected_policy.generate_from_crds),
            (
                "kubernetes.repository",
                actual.kubernetes.repository,
                expected_policy.kubernetes_repository,
            ),
            ("kubernetes.track", actual.kubernetes.track, expected_policy.kubernetes_track),
            ("catalog.repository", actual.catalog.repository, expected_policy.catalog_repository),
            ("catalog.track", actual.catalog.track, expected_policy.catalog_track),
        )
        for name, locked, authored in comparisons:
            if locked != authored:
                mismatches.append(f"{name}: lock={locked!r}, workspace={authored!r}")
        if mismatches:
            raise SchemaLockError(
                "schema lock does not match workspace policy; run "
                "`chart-manager schemas sync --update`: " + "; ".join(mismatches)
            )
        if lock.inventory != requirements:
            raise SchemaLockError(
                "rendered schema inventory differs from the lock; run "
                "`chart-manager schemas sync --update`"
            )
        self._verify_locked_materialized(lock, request.materialized)
        return lock

    def _verify_locked_materialized(
        self,
        lock: SchemaLock,
        materialized_values: tuple[MaterializedSchema, ...],
    ) -> None:
        """Reject local input drift before accepting even a warm generation."""
        materialized = _index_materialized(materialized_values)
        for entry in lock.schemas:
            if entry.source not in {"generated", "local"}:
                continue
            artifact = materialized.get(_schema_file_artifact_key(entry))
            if artifact is None:
                raise SchemaIntegrityError(
                    f"locked {entry.source} schema is not materialized: "
                    f"{entry.scope.key if entry.scope else '*'} {entry.gvk.key}"
                )
            if artifact.sha256 != entry.sha256:
                raise SchemaIntegrityError(
                    f"materialized schema drift for {entry.gvk.key}: expected "
                    f"{entry.sha256}, got {artifact.sha256}; run "
                    "`chart-manager schemas sync --update`"
                )

    def _hydrate_locked_content(
        self,
        lock: SchemaLock,
        materialized_values: tuple[MaterializedSchema, ...],
    ) -> dict[str, bytes]:
        materialized = _index_materialized(materialized_values)
        content_by_path: dict[str, bytes] = {}
        downloads: list[SchemaDownload] = []
        entries_by_key: dict[str, SchemaFile] = {}
        for entry in lock.schemas:
            if entry.source in {"generated", "local"}:
                artifact = materialized.get(_schema_file_artifact_key(entry))
                if artifact is None:
                    raise SchemaIntegrityError(
                        f"locked {entry.source} schema is not materialized: "
                        f"{entry.scope.key if entry.scope else '*'} {entry.gvk.key}"
                    )
                if artifact.sha256 != entry.sha256:
                    raise SchemaIntegrityError(
                        f"materialized schema drift for {entry.gvk.key}: "
                        f"expected {entry.sha256}, got {artifact.sha256}; run "
                        "`chart-manager schemas sync --update`"
                    )
                _validate_schema_json(artifact.content, source=artifact.source_reference)
                _put_content(content_by_path, entry.path, artifact.content)
                continue
            key = entry.path
            entries_by_key[key] = entry
            downloads.append(
                SchemaDownload(
                    key=key,
                    url=entry.source_reference,
                    expected_sha256=entry.sha256,
                )
            )
        batch = self.source_client.download_many(downloads)
        for key, content in batch.content.items():
            _validate_schema_json(content, source=entries_by_key[key].source_reference)
            _put_content(content_by_path, key, content)
        return content_by_path

    def _publish(
        self,
        request: SchemaSyncRequest,
        lock: SchemaLock,
        content: dict[str, bytes],
        *,
        replace_lock: bool,
    ) -> SchemaSyncResult:
        stage = self.store.create_stage()
        destination_existed = self.store.generation_path(lock).exists()
        try:
            entries = {entry.path: entry for entry in lock.schemas}
            if set(content) != set(entries):
                missing = sorted(set(entries) - set(content))
                extra = sorted(set(content) - set(entries))
                raise SchemaIntegrityError(
                    f"staged schema content differs from lock: missing={missing}, extra={extra}"
                )
            for path, value in content.items():
                self.store.write_stage_file(stage, entries[path], value)
            destination = self.store.publish_generation(stage, lock)
            stage = Path()
            if replace_lock:
                # The generation is immutable and complete before the lock becomes
                # authoritative. A failed replace leaves the old lock usable.
                write_schema_lock_atomic(request.lock_path, lock)
            return SchemaSyncResult(
                lock=lock,
                generation_path=destination,
                lock_updated=replace_lock,
                generation_published=not destination_existed,
            )
        finally:
            if stage != Path():
                self.store.discard_stage(stage)


def _index_materialized(
    values: tuple[MaterializedSchema, ...],
) -> dict[tuple[str, str, str, str, str, str], MaterializedSchema]:
    indexed: dict[tuple[str, str, str, str, str, str], MaterializedSchema] = {}
    for value in values:
        key = _artifact_key(value)
        previous = indexed.get(key)
        if previous is not None and previous.content != value.content:
            raise SchemaIntegrityError(
                f"conflicting {value.source} schema bytes for {value.scope.key} {value.gvk.key}"
            )
        indexed[key] = value
    return indexed


def _artifact_key(value: MaterializedSchema) -> tuple[str, str, str, str, str, str]:
    return (
        value.source,
        value.scope.chart,
        value.scope.environment or "",
        value.gvk.group,
        value.gvk.version,
        value.gvk.kind,
    )


def _schema_file_artifact_key(value: SchemaFile) -> tuple[str, str, str, str, str, str]:
    assert value.scope is not None
    return (
        value.source,
        value.scope.chart,
        value.scope.environment or "",
        value.gvk.group,
        value.gvk.version,
        value.gvk.kind,
    )


def _select_materialized(
    indexed: dict[tuple[str, str, str, str, str, str], MaterializedSchema],
    requirement: SchemaRequirement,
) -> MaterializedSchema | None:
    for source in ("generated", "local"):
        for environment in (requirement.scope.environment or "", ""):
            key = (
                source,
                requirement.scope.chart,
                environment,
                requirement.gvk.group,
                requirement.gvk.version,
                requirement.gvk.kind,
            )
            artifact = indexed.get(key)
            if artifact is not None:
                return artifact
    return None


def _unique_gvks(requirements: list[SchemaRequirement]) -> tuple[GroupVersionKind, ...]:
    unique = {_gvk_key(item.gvk): item.gvk for item in requirements}
    return tuple(sorted(unique.values(), key=lambda item: (item.group, item.version, item.kind)))


def _gvk_key(gvk: GroupVersionKind) -> tuple[str, str, str]:
    return gvk.group, gvk.version, gvk.kind


def _kubernetes_schema_url(policy: LockedSchemaPolicy, gvk: GroupVersionKind) -> str:
    suffix = "-" + "-".join(
        part.replace(".", "-") for part in ([gvk.group] if gvk.group else []) + [gvk.version]
    )
    filename = f"{gvk.kind.lower()}{suffix.lower()}.json"
    path = f"v{policy.kubernetes_version}-standalone-strict/{filename}"
    return raw_github_url(policy.kubernetes.repository, policy.kubernetes.resolved, path)


def _catalog_schema_url(policy: LockedSchemaPolicy, gvk: GroupVersionKind) -> str:
    path = f"{gvk.group}/{gvk.kind.lower()}_{gvk.version}.json"
    return raw_github_url(policy.catalog.repository, policy.catalog.resolved, path)


def _validate_schema_json(content: bytes, *, source: str) -> None:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaIntegrityError(f"schema from {source} is invalid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SchemaIntegrityError(f"schema from {source} must be a JSON object")


def _put_content(target: dict[str, bytes], path: str, content: bytes) -> None:
    previous = target.get(path)
    if previous is not None and previous != content:
        raise SchemaIntegrityError(f"two schemas resolve to the same store path: {path}")
    target[path] = content


__all__ = [
    "SchemaSyncRequest",
    "SchemaSyncResult",
    "SchemaSyncService",
]
