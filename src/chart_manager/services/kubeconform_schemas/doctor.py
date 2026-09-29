"""Read-only preflight for the authored schema policy, lock, and XDG store."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from chart_manager.domain.workspace import SCHEMA_LOCK_FILE, RepositoryWorkspace
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import Check
from chart_manager.services.kubeconform_schemas.errors import (
    KubeconformSchemaLockError,
    KubeconformSchemaStoreError,
)
from chart_manager.services.kubeconform_schemas.lock import load_schema_lock
from chart_manager.services.kubeconform_schemas.models import (
    AuthoredSchemaPolicy,
    SchemaFile,
    SchemaLock,
    SchemaRequirement,
    SchemaSourceKind,
)
from chart_manager.services.kubeconform_schemas.store import (
    KubeconformSchemaStore,
    StoreStatus,
)

_UPDATE = "run chart-manager schemas sync --update"
_HYDRATE = "run chart-manager schemas sync while online"
_SOURCES: tuple[SchemaSourceKind, ...] = (
    "generated",
    "local",
    "kubernetes",
    "catalog",
)


class KubeconformSchemaDoctor:
    """Inspect kubeconform schema readiness without network or write access."""

    def __init__(
        self,
        workspace: RepositoryWorkspace,
        *,
        cache_root: Path | None = None,
    ) -> None:
        self.workspace = workspace
        self.cache_root = cache_root

    def preflight(self) -> tuple[Check, ...]:
        """Report policy, lock, and immutable generation readiness from disk only."""
        policy_check, policy = self._policy_check()
        if policy is None or not self.workspace.name:
            return (
                policy_check,
                Check.skipped("schema-lock", "workspace schema policy is unavailable"),
                Check.skipped("schema-store", "workspace schema policy is unavailable"),
            )

        lock_check, lock = self._lock_check(policy)
        if lock is None:
            return (
                policy_check,
                lock_check,
                Check.skipped(
                    "schema-store",
                    "schema lock is unavailable; store generation cannot be selected",
                    data={"offlineReady": False},
                ),
            )

        try:
            store = KubeconformSchemaStore(
                self.workspace.name,
                cache_root=self.cache_root,
            )
            status = store.inspect(lock)
        except (OSError, KubeconformSchemaStoreError) as exc:
            return (
                policy_check,
                lock_check,
                Check.failed(
                    "schema-store",
                    f"schema store cannot be inspected: {exc}",
                    remediation=(
                        "set XDG_CACHE_HOME to an absolute readable path, then " + _HYDRATE
                    ),
                    outcome=Outcome.ENVIRONMENT,
                    data={"offlineReady": False},
                ),
            )
        policy_matches = bool(lock_check.data and lock_check.data.get("matchesPolicy", False))
        return (
            policy_check,
            lock_check,
            _store_check(lock, status, policy_matches=policy_matches),
        )

    def _policy_check(self) -> tuple[Check, AuthoredSchemaPolicy | None]:
        validation = self.workspace.validation
        if validation is None or not self.workspace.name:
            missing = (
                "metadata.name" if not self.workspace.name else "spec.validation schema policy"
            )
            return (
                Check.failed(
                    "schema-policy",
                    f"{self.workspace.marker} is missing {missing}",
                    remediation=(
                        "configure metadata.name, spec.validation.kubernetesVersion, and "
                        "spec.validation.schemas, then " + _UPDATE
                    ),
                    outcome=Outcome.SPEC,
                    data={"configured": False},
                ),
                None,
            )
        policy = AuthoredSchemaPolicy(
            kubernetes_version=validation.kubernetes_version,
            generate_from_crds=validation.schemas.generate_from_crds,
            catalog_repository=validation.schemas.catalog.repository,
            catalog_track=validation.schemas.catalog.track,
        )
        data = _policy_data(self.workspace.name, policy)
        detail = (
            f"workspace={self.workspace.name}; kubernetes={policy.normalized_version()}; "
            f"generateFromCRDs={str(policy.generate_from_crds).lower()}; "
            f"catalog={policy.catalog_repository}@{policy.catalog_track}"
        )
        return Check.ok("schema-policy", detail, data=data), policy

    def _lock_check(
        self,
        policy: AuthoredSchemaPolicy,
    ) -> tuple[Check, SchemaLock | None]:
        path = self.workspace.root / SCHEMA_LOCK_FILE
        if not path.is_file():
            return (
                Check.failed(
                    "schema-lock",
                    f"schema lock does not exist: {path}",
                    remediation=_UPDATE,
                    outcome=Outcome.SPEC,
                    data={"path": str(path), "present": False, "matchesPolicy": False},
                ),
                None,
            )
        try:
            lock = load_schema_lock(path)
        except KubeconformSchemaLockError as exc:
            return (
                Check.failed(
                    "schema-lock",
                    str(exc),
                    remediation=_UPDATE,
                    outcome=Outcome.SPEC,
                    data={"path": str(path), "present": True, "matchesPolicy": False},
                ),
                None,
            )
        mismatches = _policy_mismatches(self.workspace.name or "", policy, lock)
        data: dict[str, Any] = {
            "path": str(path),
            "present": True,
            "workspace": lock.workspace,
            "generation": lock.generation,
            "matchesPolicy": not mismatches,
            "mismatches": list(mismatches),
            "inventory": len(lock.inventory),
            "schemas": len(lock.schemas),
        }
        if mismatches:
            return (
                Check.failed(
                    "schema-lock",
                    "schema lock does not match workspace policy: " + "; ".join(mismatches),
                    remediation=_UPDATE,
                    outcome=Outcome.SPEC,
                    data=data,
                ),
                lock,
            )
        return (
            Check.ok(
                "schema-lock",
                f"{path}; generation={lock.generation}; policy matches; "
                f"inventory={len(lock.inventory)} schemas={len(lock.schemas)}",
                data=data,
            ),
            lock,
        )


def _store_check(
    lock: SchemaLock,
    status: StoreStatus,
    *,
    policy_matches: bool,
) -> Check:
    unavailable = {problem.path for problem in (*status.missing, *status.corrupt)}
    missing_gvks = _missing_gvks(lock, unavailable)
    source_coverage = _source_coverage(lock)
    data: dict[str, Any] = {
        "path": str(status.generation_path),
        "generation": lock.generation,
        "expected": status.expected,
        "present": status.present,
        "missing": len(status.missing),
        "corrupt": len(status.corrupt),
        "uncovered": len(status.uncovered),
        "sourceCoverage": source_coverage,
        "missingGVKs": missing_gvks,
        "ready": status.ready,
        "offlineReady": status.ready and policy_matches,
    }
    sources = ", ".join(f"{source}={source_coverage[source]['files']}" for source in _SOURCES)
    detail = (
        f"{status.generation_path}; generation={lock.generation}; "
        f"expected={status.expected} present={status.present} "
        f"missing={len(status.missing)} corrupt={len(status.corrupt)} "
        f"uncovered={len(status.uncovered)}; sources: {sources}; "
        f"offline-ready={str(status.ready and policy_matches).lower()}"
    )
    if missing_gvks:
        detail += "; missing GVKs: " + ", ".join(
            f"{item['scope']} {item['gvk']}" for item in missing_gvks
        )
    if status.ready:
        return Check.ok("schema-store", detail, data=data)
    if status.uncovered:
        return Check.failed(
            "schema-store",
            detail,
            remediation=(
                _UPDATE + "; if a schema is intentionally unavailable, add its kind to "
                "spec.validation.ignoreMissingSchemas"
            ),
            outcome=Outcome.SPEC,
            data=data,
        )
    if status.corrupt:
        return Check.failed(
            "schema-store",
            detail,
            remediation=f"remove {status.generation_path}, then {_HYDRATE}",
            outcome=Outcome.TOOL,
            data=data,
        )
    return Check.failed(
        "schema-store",
        detail,
        remediation=_HYDRATE,
        outcome=Outcome.ENVIRONMENT,
        data=data,
    )


def _policy_data(workspace: str, policy: AuthoredSchemaPolicy) -> dict[str, Any]:
    return {
        "configured": True,
        "workspace": workspace,
        "kubernetesVersion": policy.normalized_version(),
        "generateFromCRDs": policy.generate_from_crds,
        "kubernetes": {
            "repository": policy.kubernetes_repository,
            "track": policy.kubernetes_track,
        },
        "catalog": {
            "repository": policy.catalog_repository,
            "track": policy.catalog_track,
        },
    }


def _policy_mismatches(
    workspace: str,
    policy: AuthoredSchemaPolicy,
    lock: SchemaLock,
) -> tuple[str, ...]:
    comparisons = (
        ("workspace", lock.workspace, workspace),
        ("kubernetesVersion", lock.policy.kubernetes_version, policy.normalized_version()),
        ("generateFromCRDs", lock.policy.generate_from_crds, policy.generate_from_crds),
        (
            "kubernetes.repository",
            lock.policy.kubernetes.repository,
            policy.kubernetes_repository,
        ),
        ("kubernetes.track", lock.policy.kubernetes.track, policy.kubernetes_track),
        ("catalog.repository", lock.policy.catalog.repository, policy.catalog_repository),
        ("catalog.track", lock.policy.catalog.track, policy.catalog_track),
    )
    return tuple(
        f"{name}: lock={locked!r}, workspace={authored!r}"
        for name, locked, authored in comparisons
        if locked != authored
    )


def _source_coverage(lock: SchemaLock) -> dict[str, dict[str, int]]:
    files = Counter(entry.source for entry in lock.schemas)
    requirements = Counter[str]()
    for requirement in lock.inventory:
        for source in {entry.source for entry in lock.schemas if _entry_covers(entry, requirement)}:
            requirements[source] += 1
    return {
        source: {
            "files": files[source],
            "requirements": requirements[source],
        }
        for source in _SOURCES
    }


def _missing_gvks(
    lock: SchemaLock,
    unavailable_paths: set[str],
) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for requirement in lock.inventory:
        candidates = [entry for entry in lock.schemas if _entry_covers(entry, requirement)]
        if requirement.allow_missing:
            continue
        if not candidates or all(entry.path in unavailable_paths for entry in candidates):
            missing.append(
                {
                    "scope": requirement.scope.key,
                    "gvk": requirement.gvk.key,
                }
            )
    return missing


def _entry_covers(entry: SchemaFile, requirement: SchemaRequirement) -> bool:
    return entry.gvk == requirement.gvk and (
        entry.scope is None or entry.scope.covers(requirement.scope)
    )


__all__ = ["KubeconformSchemaDoctor"]
