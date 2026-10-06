"""Read-only preflight for the authored schema policy, lock, and XDG store."""

from __future__ import annotations

from typing import Any

from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaLockError,
    KubeconformSchemaStoreError,
)
from chart_manager.commands.validate.schemas.lock import load_schema_lock
from chart_manager.commands.validate.schemas.models import (
    AuthoredSchemaPolicy,
    SchemaLock,
    lock_policy_mismatches,
)
from chart_manager.commands.validate.schemas.store import (
    StoreStatus,
    open_schema_store,
)
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import Check
from chart_manager.shared.workspace import SCHEMA_LOCK_FILE, RepositoryWorkspace

_UPDATE = "run chart-manager schemas sync --update"
_HYDRATE = "run chart-manager schemas sync"


class KubeconformSchemaDoctor:
    """Inspect kubeconform schema readiness without network or write access."""

    def __init__(
        self,
        workspace: RepositoryWorkspace | None,
        *,
        runner: CommandRunner,
        skip_reason: str = "no workspace",
    ) -> None:
        self.workspace = workspace
        self.runner = runner
        self.skip_reason = skip_reason

    def preflight(self) -> tuple[Check, ...]:
        """Report policy, lock, and immutable generation readiness from disk only.

        Without a workspace every check is skipped with `skip_reason`.
        """
        if self.workspace is None:
            return tuple(
                Check.skipped(name, self.skip_reason)
                for name in ("schema-policy", "schema-lock", "schema-store")
            )
        policy_check, policy = self._policy_check(self.workspace)
        if policy is None:
            return (
                policy_check,
                Check.skipped("schema-lock", "workspace schema policy is unavailable"),
                Check.skipped("schema-store", "workspace schema policy is unavailable"),
            )

        lock_check, lock = self._lock_check(self.workspace, policy)
        if lock is None:
            return (
                policy_check,
                lock_check,
                Check.skipped(
                    "schema-store",
                    "schema lock is unavailable; store generation cannot be selected",
                    data={"ready": False},
                ),
            )

        try:
            status = open_schema_store(self.runner).inspect(lock)
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
                    data={"ready": False},
                ),
            )
        policy_matches = bool(lock_check.data and lock_check.data.get("matchesPolicy", False))
        return (
            policy_check,
            lock_check,
            _store_check(lock, status, policy_matches=policy_matches),
        )

    @staticmethod
    def _policy_check(
        workspace: RepositoryWorkspace,
    ) -> tuple[Check, AuthoredSchemaPolicy | None]:
        validation = workspace.spec.validation
        if validation is None:
            return (
                Check.failed(
                    "schema-policy",
                    f"{workspace.marker} is missing spec.validation schema policy",
                    remediation=(
                        "configure spec.validation.kubernetesVersion and "
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
        data = _policy_data(workspace.name, policy)
        detail = (
            f"workspace={workspace.name}; kubernetes={policy.normalized_version()}; "
            f"generateFromCRDs={str(policy.generate_from_crds).lower()}; "
            f"catalog={policy.catalog_repository}@{policy.catalog_track}"
        )
        return Check.ok("schema-policy", detail, data=data), policy

    @staticmethod
    def _lock_check(
        workspace: RepositoryWorkspace,
        policy: AuthoredSchemaPolicy,
    ) -> tuple[Check, SchemaLock | None]:
        path = workspace.root / SCHEMA_LOCK_FILE
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
        mismatches = _policy_mismatches(workspace.name, policy, lock)
        data: dict[str, Any] = {
            "path": str(path),
            "present": True,
            "workspace": lock.workspace,
            "generation": lock.generation,
            "matchesPolicy": not mismatches,
            "mismatches": list(mismatches),
            "repositories": 2,
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
                f"{path}; generation={lock.generation}; policy matches; repositories=2",
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
    data: dict[str, Any] = {
        "path": str(status.generation_path),
        "generation": lock.generation,
        "expected": status.expected,
        "present": status.present,
        "missing": len(status.missing),
        "corrupt": len(status.corrupt),
        "ready": status.ready and policy_matches,
        "generatedSchemas": "automatically prepared during validate",
    }
    detail = (
        f"{status.generation_path}; generation={lock.generation}; "
        f"expected={status.expected} present={status.present} "
        f"missing={len(status.missing)} corrupt={len(status.corrupt)}; "
        f"ready={str(status.ready and policy_matches).lower()}; "
        "generated CRD schemas are prepared automatically during validate"
    )
    if status.ready:
        return Check.ok("schema-store", detail, data=data)
    if status.corrupt:
        return Check.failed(
            "schema-store",
            detail,
            remediation="remove affected snapshot(s): "
            + "; ".join(p.path for p in status.corrupt)
            + f", then {_HYDRATE}",
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
    return lock_policy_mismatches(policy, lock, workspace=workspace)


__all__ = ["KubeconformSchemaDoctor"]
