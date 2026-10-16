"""Typed failures for kubeconform schema preparation and storage."""

from __future__ import annotations

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.exit_codes import Outcome


class KubeconformSchemaError(ChartManagerError):
    """Base class for expected kubeconform schema subsystem failures."""

    outcome = Outcome.TOOL


class KubeconformSchemaConfigurationError(KubeconformSchemaError):
    """The authored schema policy or rendered CRD inputs are inconsistent."""

    outcome = Outcome.SPEC


class KubeconformSchemaIntegrityError(KubeconformSchemaError):
    """Locked or materialized schema content failed an integrity check."""


class KubeconformSchemaLockError(KubeconformSchemaIntegrityError):
    """The committed schema lock is missing, malformed, or inconsistent."""

    outcome = Outcome.SPEC


class KubeconformSchemaStoreError(KubeconformSchemaIntegrityError):
    """An immutable store generation is incomplete or corrupt."""


class KubeconformSchemaSourceEnvironmentError(KubeconformSchemaError):
    """A schema source could not be reached from the caller's environment."""

    outcome = Outcome.ENVIRONMENT


__all__ = [
    "KubeconformSchemaConfigurationError",
    "KubeconformSchemaError",
    "KubeconformSchemaIntegrityError",
    "KubeconformSchemaLockError",
    "KubeconformSchemaSourceEnvironmentError",
    "KubeconformSchemaStoreError",
]
