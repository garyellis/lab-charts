"""Typed failures for kubeconform schema preparation and storage."""

from __future__ import annotations

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.exit_codes import Outcome


class KubeconformSchemaError(ChartManagerError):
    """Base class for expected kubeconform schema subsystem failures."""


class KubeconformSchemaConfigurationError(KubeconformSchemaError):
    """The authored schema policy or rendered CRD inputs are inconsistent."""


class KubeconformSchemaRenderError(KubeconformSchemaError):
    """CRD provider rendering failed; preserve the validation runner's classification."""

    def __init__(self, message: str, *, outcome: Outcome) -> None:
        super().__init__(message)
        self.outcome = outcome


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


__all__ = [
    "KubeconformSchemaConfigurationError",
    "KubeconformSchemaError",
    "KubeconformSchemaIntegrityError",
    "KubeconformSchemaLockError",
    "KubeconformSchemaRenderError",
    "KubeconformSchemaSourceEnvironmentError",
    "KubeconformSchemaSourceError",
    "KubeconformSchemaStoreError",
]
