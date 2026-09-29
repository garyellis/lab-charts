"""Typed failures for kubeconform schema preparation and storage."""

from __future__ import annotations

from chart_manager.plumbing.errors import ChartManagerError


class KubeconformSchemaError(ChartManagerError):
    """Base class for expected kubeconform schema subsystem failures."""


class KubeconformSchemaConfigurationError(KubeconformSchemaError):
    """The authored schema policy or requested inventory is inconsistent."""


class KubeconformSchemaIntegrityError(KubeconformSchemaError):
    """Locked or materialized schema content failed an integrity check."""


class KubeconformSchemaLockError(KubeconformSchemaIntegrityError):
    """The committed schema lock is missing, malformed, or inconsistent."""


class KubeconformSchemaStoreError(KubeconformSchemaIntegrityError):
    """An immutable store generation is incomplete or corrupt."""


class KubeconformSchemaSourceError(KubeconformSchemaError):
    """Base class for immutable schema-source access failures."""


class KubeconformSchemaSourceEnvironmentError(KubeconformSchemaSourceError):
    """A schema source could not be reached from the caller's environment."""


class KubeconformSchemaSourceIntegrityError(KubeconformSchemaSourceError):
    """A schema source responded with malformed or unexpected content."""


class KubeconformSchemaNotFoundError(KubeconformSchemaSourceError):
    """A requested immutable schema artifact does not exist."""


__all__ = [
    "KubeconformSchemaConfigurationError",
    "KubeconformSchemaError",
    "KubeconformSchemaIntegrityError",
    "KubeconformSchemaLockError",
    "KubeconformSchemaNotFoundError",
    "KubeconformSchemaSourceEnvironmentError",
    "KubeconformSchemaSourceError",
    "KubeconformSchemaSourceIntegrityError",
    "KubeconformSchemaStoreError",
]
