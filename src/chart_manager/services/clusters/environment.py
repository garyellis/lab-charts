"""Kubernetes environment provider boundary for local-cluster orchestration.

Local convergence needs a Kubernetes API, not Kind specifically. Providers
own environment lifecycle and return the concrete identity/context that every
cluster-facing client must use.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from chart_manager.integrations.kind import Kind, kind_context


@dataclass(frozen=True)
class EnvironmentSpec:
    """Desired local Kubernetes environment."""

    name: str
    cluster_name: str
    config: Path | None = None


@dataclass(frozen=True)
class EnvironmentHandle:
    """Resolved environment identity returned by a provider."""

    identity: str
    context: str
    provider_type: str


@runtime_checkable
class KubernetesEnvironmentProvider(Protocol):
    """Provisioning contract used by local-cluster orchestration."""

    def ensure(self, spec: EnvironmentSpec) -> EnvironmentHandle:
        """Create/start an environment and return its addressable handle."""

    def stop(self, handle: EnvironmentHandle) -> bool:
        """Stop an environment while preserving recoverable state."""

    def destroy(self, handle: EnvironmentHandle) -> bool:
        """Destroy an environment and its provider-owned state."""

    def inspect(self, spec: EnvironmentSpec) -> EnvironmentHandle | None:
        """Return a handle when the environment exists."""

    def handle(self, spec: EnvironmentSpec) -> EnvironmentHandle:
        """Return the stable addressable identity whether or not it exists."""


class KindEnvironmentProvider:
    """Kind implementation of :class:`KubernetesEnvironmentProvider`."""

    provider_type = "kind"

    def __init__(self, kind: Kind) -> None:
        self.kind = kind

    def ensure(self, spec: EnvironmentSpec) -> EnvironmentHandle:
        self.kind.ensure_cluster(spec.cluster_name, config=spec.config)
        return self._handle(spec.cluster_name)

    def stop(self, handle: EnvironmentHandle) -> bool:
        return self.kind.stop_cluster(handle.identity)

    def destroy(self, handle: EnvironmentHandle) -> bool:
        return self.kind.delete_cluster(handle.identity)

    def inspect(self, spec: EnvironmentSpec) -> EnvironmentHandle | None:
        if spec.cluster_name not in self.kind.clusters():
            return None
        return self._handle(spec.cluster_name)

    def handle(self, spec: EnvironmentSpec) -> EnvironmentHandle:
        return self._handle(spec.cluster_name)

    @staticmethod
    def _handle(cluster_name: str) -> EnvironmentHandle:
        return EnvironmentHandle(
            identity=cluster_name,
            context=kind_context(cluster_name),
            provider_type="kind",
        )


__all__ = [
    "EnvironmentHandle",
    "EnvironmentSpec",
    "KindEnvironmentProvider",
    "KubernetesEnvironmentProvider",
]
