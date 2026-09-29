"""Typed failures for schema inventory, locking, synchronization, and storage."""

from __future__ import annotations

from chart_manager.integrations.schema_sources import (
    SchemaNotFoundError,
    SchemaSourceEnvironmentError,
    SchemaSourceError,
    SchemaSourceIntegrityError,
)
from chart_manager.plumbing.errors import ChartManagerError


class SchemaError(ChartManagerError):
    """Base class for expected schema subsystem failures."""


class SchemaConfigurationError(SchemaError):
    """The authored schema policy or requested inventory is inconsistent."""


class SchemaIntegrityError(SchemaError):
    """Locked or materialized schema content failed an integrity check."""


class SchemaLockError(SchemaIntegrityError):
    """The committed schema lock is missing, malformed, or inconsistent."""


class SchemaStoreError(SchemaIntegrityError):
    """An immutable store generation is incomplete or corrupt."""


__all__ = [
    "SchemaConfigurationError",
    "SchemaError",
    "SchemaIntegrityError",
    "SchemaLockError",
    "SchemaNotFoundError",
    "SchemaSourceEnvironmentError",
    "SchemaSourceError",
    "SchemaSourceIntegrityError",
    "SchemaStoreError",
]
