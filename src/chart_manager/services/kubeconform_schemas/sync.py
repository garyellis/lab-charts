"""Eager schema synchronization against either tracking refs or an existing lock."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from chart_manager.integrations.kubeconform.github_schema_source import (
    GitHubKubeconformSchemaNotFoundError,
    GitHubKubeconformSchemaSourceEnvironmentError,
    GitHubKubeconformSchemaSourceError,
    GitHubKubeconformSchemaSourceIntegrityError,
)
from chart_manager.services.kubeconform_schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaIntegrityError,
    KubeconformSchemaLockError,
    KubeconformSchemaNotFoundError,
    KubeconformSchemaSourceEnvironmentError,
    KubeconformSchemaSourceError,
    KubeconformSchemaSourceIntegrityError,
)
from chart_manager.services.kubeconform_schemas.lock import (
    load_schema_lock,
    write_schema_lock_atomic,
)
from chart_manager.services.kubeconform_schemas.models import (
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
    lock_policy_mismatches,
    sort_requirements,
    sort_schema_files,
)
from chart_manager.services.kubeconform_schemas.source import (
    KubeconformSchemaArtifactRequest,
    KubeconformSchemaSource,
)
from chart_manager.services.kubeconform_schemas.store import (
    KubeconformSchemaLocations,
    KubeconformSchemaStore,
    artifact_relative_path,
    kubeconform_schema_locations,
)


@dataclass(frozen=True)
class KubeconformSchemaSyncRequest:
    workspace: str
    policy: AuthoredSchemaPolicy
    requirements: tuple[SchemaRequirement, ...]
    materialized: tuple[MaterializedSchema, ...]
    lock_path: Path
    update: bool = False


@dataclass(frozen=True)
class KubeconformSchemaSyncResult:
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


class KubeconformSchemaSyncService:
    """Build complete immutable generations without exposing partial state."""

    def __init__(
        self,
        store: KubeconformSchemaStore,
        source: KubeconformSchemaSource,
    ) -> None:
        self.store = store
        self.source = source

    def sync(
        self,
        request: KubeconformSchemaSyncRequest,
    ) -> KubeconformSchemaSyncResult:
        """Verify current derived inputs, then return a ready generation.

        With ``update=False`` the committed lock is read-only: even a cold-cache
        hydration publishes only the lock's immutable generation. Discovery is
        authoritative and happens before cache inspection, so a warm cache cannot
        conceal a stale lock. Only an explicit update resolves tracking refs and
        replaces the repository lock.
        """
        if request.workspace != self.store.workspace:
            raise KubeconformSchemaConfigurationError(
                f"sync workspace {request.workspace!r} does not match store "
                f"workspace {self.store.workspace!r}"
            )
        try:
            return self._sync(request)
        except GitHubKubeconformSchemaSourceEnvironmentError as exc:
            raise KubeconformSchemaSourceEnvironmentError(str(exc)) from exc
        except GitHubKubeconformSchemaNotFoundError as exc:
            raise KubeconformSchemaNotFoundError(str(exc)) from exc
        except GitHubKubeconformSchemaSourceIntegrityError as exc:
            raise KubeconformSchemaSourceIntegrityError(str(exc)) from exc
        except GitHubKubeconformSchemaSourceError as exc:
            raise KubeconformSchemaSourceError(str(exc)) from exc

    def _sync(
        self,
        request: KubeconformSchemaSyncRequest,
    ) -> KubeconformSchemaSyncResult:
        """Synchronize after translating adapter failures at the public boundary."""
        requirements = sort_requirements(list(request.requirements))
        with self.store.serialized_sync():
            if request.update:
                lock, content = self._build_updated_lock(request, requirements)
                return self._publish(
                    request,
                    lock,
                    content,
                    replace_lock=True,
                )
            lock = self._load_existing(request)
            self._verify_current_derivations(
                lock,
                requirements,
                request.materialized,
            )
            status = self.store.inspect(lock)
            if status.ready:
                return KubeconformSchemaSyncResult(
                    lock=lock,
                    generation_path=status.generation_path,
                    lock_updated=False,
                    generation_published=False,
                )
            if status.corrupt:
                # Corruption is a local tool/store failure. Do not disguise it
                # as a source problem by downloading before reporting it.
                self.store.require_ready(lock)
            content = self._hydrate_locked_content(
                lock,
                request.materialized,
            )
            return self._publish(
                request,
                lock,
                content,
                replace_lock=False,
            )

    def refresh(
        self,
        request: KubeconformSchemaSyncRequest,
    ) -> KubeconformSchemaSyncResult:
        """Rebuild derived requirements against the already committed pins.

        Unlike ``sync(update=True)``, this operation never resolves moving
        refs. Local chart evolution and upstream schema movement are separate
        review events.
        """
        if request.workspace != self.store.workspace:
            raise KubeconformSchemaConfigurationError(
                f"sync workspace {request.workspace!r} does not match store "
                f"workspace {self.store.workspace!r}"
            )
        try:
            requirements = sort_requirements(list(request.requirements))
            with self.store.serialized_sync():
                existing = self._load_existing(request)
                lock, content = self._build_lock_for_policy(
                    request,
                    requirements,
                    existing.policy,
                )
                return self._publish(
                    request,
                    lock,
                    content,
                    replace_lock=lock != existing,
                )
        except GitHubKubeconformSchemaSourceEnvironmentError as exc:
            raise KubeconformSchemaSourceEnvironmentError(str(exc)) from exc
        except GitHubKubeconformSchemaNotFoundError as exc:
            raise KubeconformSchemaNotFoundError(str(exc)) from exc
        except GitHubKubeconformSchemaSourceIntegrityError as exc:
            raise KubeconformSchemaSourceIntegrityError(str(exc)) from exc
        except GitHubKubeconformSchemaSourceError as exc:
            raise KubeconformSchemaSourceError(str(exc)) from exc

    def _build_updated_lock(
        self,
        request: KubeconformSchemaSyncRequest,
        requirements: tuple[SchemaRequirement, ...],
    ) -> tuple[SchemaLock, dict[str, bytes]]:
        policy = request.policy
        kubernetes_resolved = self.source.resolve_ref(
            policy.kubernetes_repository, policy.kubernetes_track
        )
        catalog_resolved = self.source.resolve_ref(policy.catalog_repository, policy.catalog_track)
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

        return self._build_lock_for_policy(request, requirements, locked_policy)

    def _build_lock_for_policy(
        self,
        request: KubeconformSchemaSyncRequest,
        requirements: tuple[SchemaRequirement, ...],
        locked_policy: LockedSchemaPolicy,
    ) -> tuple[SchemaLock, dict[str, bytes]]:
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
        # Optional-only GVKs never cause remote discovery. They are validated
        # when a derived schema or a remote schema required elsewhere exists.
        unresolved_gvks = _unique_gvks(
            [requirement for requirement in unresolved if not requirement.allow_missing]
        )
        kubernetes_requests = [
            KubeconformSchemaArtifactRequest(
                key=gvk.key,
                url=_kubernetes_schema_url(self.source, locked_policy, gvk),
            )
            for gvk in unresolved_gvks
        ]
        kubernetes_batch = self.source.fetch_many(
            kubernetes_requests,
            allow_not_found=True,
        )
        for gvk in unresolved_gvks:
            content = kubernetes_batch.content.get(gvk.key)
            if content is not None:
                _validate_schema_json(
                    content,
                    source=_kubernetes_schema_url(self.source, locked_policy, gvk),
                )
                remote_by_gvk[_gvk_key(gvk)] = (
                    "kubernetes",
                    content,
                    _kubernetes_schema_url(self.source, locked_policy, gvk),
                )

        kubernetes_missing = set(kubernetes_batch.missing)
        catalog_gvks = [
            gvk for gvk in unresolved_gvks if gvk.key in kubernetes_missing and gvk.group
        ]
        catalog_requests = [
            KubeconformSchemaArtifactRequest(
                key=gvk.key,
                url=_catalog_schema_url(self.source, locked_policy, gvk),
            )
            for gvk in catalog_gvks
        ]
        catalog_batch = self.source.fetch_many(
            catalog_requests,
            allow_not_found=True,
        )
        for gvk in catalog_gvks:
            content = catalog_batch.content.get(gvk.key)
            if content is not None:
                _validate_schema_json(
                    content,
                    source=_catalog_schema_url(self.source, locked_policy, gvk),
                )
                remote_by_gvk[_gvk_key(gvk)] = (
                    "catalog",
                    content,
                    _catalog_schema_url(self.source, locked_policy, gvk),
                )

        missing = [
            requirement
            for requirement in unresolved
            if _gvk_key(requirement.gvk) not in remote_by_gvk and not requirement.allow_missing
        ]
        if missing:
            detail = ", ".join(sorted(f"{item.scope.key}: {item.gvk.key}" for item in missing))
            raise KubeconformSchemaConfigurationError(
                "required schemas were not found in generated, local, Kubernetes, "
                f"or catalog sources: {detail}; add an exact local schema or "
                "spec.validation.ignoreMissingSchemas entry in the chart's chart-lifecycle.yaml"
            )

        files: list[SchemaFile] = []
        content_by_path: dict[str, bytes] = {}
        for artifact in selected.values():
            _validate_schema_json(artifact.content, source=artifact.source_reference)
            scope = None if artifact.source == "generated" else artifact.scope
            path = artifact_relative_path(
                source=artifact.source,
                gvk=artifact.gvk,
                scope=scope,
            )
            entry = SchemaFile(
                gvk=artifact.gvk,
                source=artifact.source,
                scope=scope,
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
            schemas=files,
        )
        return lock, content_by_path

    def _verify_current_derivations(
        self,
        lock: SchemaLock,
        requirements: tuple[SchemaRequirement, ...],
        materialized_values: tuple[MaterializedSchema, ...],
    ) -> None:
        """Compare current repository-derived requirements with the compact lock.

        Remote entries can be verified from their locked identities without any
        source access. Generated and local entries are reconstructed from the
        current checkout and compared byte-for-byte. Scope fan-out is deliberately
        absent: another use of an already-covered repository schema is harmless.
        """
        materialized = _index_materialized(materialized_values)
        strict_remote_gvks = {
            _gvk_key(requirement.gvk)
            for requirement in requirements
            if not requirement.allow_missing
            and _select_materialized(materialized, requirement) is None
        }
        remote_by_gvk: dict[tuple[str, str, str], list[SchemaFile]] = {}
        for entry in lock.schemas:
            if entry.source in {"kubernetes", "catalog"}:
                remote_by_gvk.setdefault(_gvk_key(entry.gvk), []).append(entry)

        expected: list[SchemaFile] = []
        problems: list[str] = []
        for requirement in requirements:
            artifact = _select_materialized(materialized, requirement)
            if artifact is not None:
                _validate_schema_json(artifact.content, source=artifact.source_reference)
                scope = artifact.scope if artifact.source == "local" else None
                expected.append(
                    SchemaFile(
                        gvk=artifact.gvk,
                        source=artifact.source,
                        scope=scope,
                        path=artifact_relative_path(
                            source=artifact.source,
                            gvk=artifact.gvk,
                            scope=scope,
                        ),
                        sha256=artifact.sha256,
                        source_reference=artifact.source_reference,
                    )
                )
                continue

            remote = (
                remote_by_gvk.get(_gvk_key(requirement.gvk), [])
                if _gvk_key(requirement.gvk) in strict_remote_gvks
                else []
            )
            if len(remote) == 1:
                expected.append(remote[0])
                continue
            if len(remote) > 1:
                problems.append(f"{requirement.gvk.key} has multiple locked remote sources")
                continue
            if not requirement.allow_missing:
                problems.append(f"{requirement.scope.key}: {requirement.gvk.key} is not covered")

        try:
            ordered_expected = sort_schema_files(expected)
        except KubeconformSchemaConfigurationError as exc:
            problems.append(str(exc))
            ordered_expected = ()
        if ordered_expected != lock.schemas:
            expected_keys = {_schema_identity(entry): entry for entry in ordered_expected}
            locked_keys = {_schema_identity(entry): entry for entry in lock.schemas}
            for key in sorted(expected_keys.keys() - locked_keys.keys()):
                problems.append(f"new {expected_keys[key].source} schema {key}")
            for key in sorted(locked_keys.keys() - expected_keys.keys()):
                problems.append(f"unused locked {locked_keys[key].source} schema {key}")
            for key in sorted(expected_keys.keys() & locked_keys.keys()):
                expected_entry = expected_keys[key]
                locked_entry = locked_keys[key]
                if expected_entry != locked_entry:
                    problems.append(
                        f"changed {expected_entry.source} schema {key} "
                        f"({locked_entry.sha256} -> {expected_entry.sha256})"
                    )
        if problems:
            detail = "; ".join(dict.fromkeys(problems))
            raise KubeconformSchemaConfigurationError(
                "schema lock is stale for the current charts; run "
                f"`chart-manager schemas sync --refresh`: {detail}"
            )

    def _load_existing(
        self,
        request: KubeconformSchemaSyncRequest,
    ) -> SchemaLock:
        if not request.lock_path.is_file():
            raise KubeconformSchemaLockError(
                f"schema lock does not exist: {request.lock_path}; "
                "run `chart-manager schemas sync --update`"
            )
        lock = load_schema_lock(request.lock_path)
        if lock.workspace != request.workspace:
            raise KubeconformSchemaLockError(
                f"schema lock is for workspace {lock.workspace!r}, not {request.workspace!r}"
            )
        mismatches = lock_policy_mismatches(request.policy, lock)
        if mismatches:
            raise KubeconformSchemaLockError(
                "schema lock does not match workspace policy; run "
                "`chart-manager schemas sync --update`: " + "; ".join(mismatches)
            )
        return lock

    def _hydrate_locked_content(
        self,
        lock: SchemaLock,
        materialized_values: tuple[MaterializedSchema, ...],
    ) -> dict[str, bytes]:
        materialized = _index_materialized(materialized_values)
        content_by_path: dict[str, bytes] = {}
        downloads: list[KubeconformSchemaArtifactRequest] = []
        entries_by_key: dict[str, SchemaFile] = {}
        for entry in lock.schemas:
            if entry.source in {"generated", "local"}:
                artifact = materialized.get(_schema_file_artifact_key(entry))
                if artifact is None:
                    raise KubeconformSchemaConfigurationError(
                        f"locked {entry.source} schema is not materialized: "
                        f"{entry.scope.key if entry.scope else 'repository'} {entry.gvk.key}; "
                        "run `chart-manager schemas sync --refresh` to rebuild derived schemas"
                    )
                if artifact.sha256 != entry.sha256:
                    raise KubeconformSchemaIntegrityError(
                        f"materialized schema drift for {entry.gvk.key}: "
                        f"expected {entry.sha256}, got {artifact.sha256}; run "
                        "`chart-manager schemas sync --refresh`"
                    )
                _validate_schema_json(artifact.content, source=artifact.source_reference)
                _put_content(content_by_path, entry.path, artifact.content)
                continue
            key = entry.path
            entries_by_key[key] = entry
            downloads.append(
                KubeconformSchemaArtifactRequest(
                    key=key,
                    url=entry.source_reference,
                    expected_sha256=entry.sha256,
                )
            )
        batch = self.source.fetch_many(downloads)
        for key, content in batch.content.items():
            _validate_schema_json(content, source=entries_by_key[key].source_reference)
            _put_content(content_by_path, key, content)
        return content_by_path

    def _publish(
        self,
        request: KubeconformSchemaSyncRequest,
        lock: SchemaLock,
        content: dict[str, bytes],
        *,
        replace_lock: bool,
    ) -> KubeconformSchemaSyncResult:
        stage = self.store.create_stage()
        destination_existed = self.store.generation_path(lock).exists()
        try:
            entries = {entry.path: entry for entry in lock.schemas}
            expected_paths = set(entries)
            if set(content) != expected_paths:
                missing = sorted(expected_paths - set(content))
                extra = sorted(set(content) - expected_paths)
                raise KubeconformSchemaIntegrityError(
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
            return KubeconformSchemaSyncResult(
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
            raise KubeconformSchemaIntegrityError(
                f"conflicting {value.source} schema bytes for "
                f"{value.scope.key if value.scope else 'repository'} {value.gvk.key}"
            )
        indexed[key] = value
    return indexed


def _artifact_key(value: MaterializedSchema) -> tuple[str, str, str, str, str, str]:
    scope = None if value.source == "generated" else value.scope
    return (
        value.source,
        scope.chart if scope else "",
        scope.environment or "" if scope else "",
        value.gvk.group,
        value.gvk.version,
        value.gvk.kind,
    )


def _schema_file_artifact_key(value: SchemaFile) -> tuple[str, str, str, str, str, str]:
    return (
        value.source,
        value.scope.chart if value.scope else "",
        value.scope.environment or "" if value.scope else "",
        value.gvk.group,
        value.gvk.version,
        value.gvk.kind,
    )


def _schema_identity(value: SchemaFile) -> str:
    scope = value.scope.key if value.scope else "repository"
    return f"{scope} {value.gvk.key}"


def _select_materialized(
    indexed: dict[tuple[str, str, str, str, str, str], MaterializedSchema],
    requirement: SchemaRequirement,
) -> MaterializedSchema | None:
    generated = indexed.get(
        (
            "generated",
            "",
            "",
            requirement.gvk.group,
            requirement.gvk.version,
            requirement.gvk.kind,
        )
    )
    if generated is not None:
        return generated
    for source in ("local",):
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


def _kubernetes_schema_url(
    source: KubeconformSchemaSource,
    policy: LockedSchemaPolicy,
    gvk: GroupVersionKind,
) -> str:
    # kubernetes-json-schema filenames use the API group prefix (``rbac``),
    # not the fully-qualified DNS group (``rbac.authorization.k8s.io``).
    group = gvk.group.split(".", 1)[0] if gvk.group else ""
    suffix = "-" + "-".join(part for part in ([group] if group else []) + [gvk.version])
    filename = f"{gvk.kind.lower()}{suffix.lower()}.json"
    path = f"v{policy.kubernetes_version}-standalone-strict/{filename}"
    return source.artifact_url(
        policy.kubernetes.repository,
        policy.kubernetes.resolved,
        path,
    )


def _catalog_schema_url(
    source: KubeconformSchemaSource,
    policy: LockedSchemaPolicy,
    gvk: GroupVersionKind,
) -> str:
    path = f"{gvk.group}/{gvk.kind.lower()}_{gvk.version}.json"
    return source.artifact_url(
        policy.catalog.repository,
        policy.catalog.resolved,
        path,
    )


def _validate_schema_json(content: bytes, *, source: str) -> None:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KubeconformSchemaIntegrityError(
            f"schema from {source} is invalid JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise KubeconformSchemaIntegrityError(f"schema from {source} must be a JSON object")


def _put_content(target: dict[str, bytes], path: str, content: bytes) -> None:
    previous = target.get(path)
    if previous is not None and previous != content:
        raise KubeconformSchemaIntegrityError(f"two schemas resolve to the same store path: {path}")
    target[path] = content


__all__ = [
    "KubeconformSchemaSyncRequest",
    "KubeconformSchemaSyncResult",
    "KubeconformSchemaSyncService",
]
