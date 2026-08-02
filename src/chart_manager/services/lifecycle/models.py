"""Immutable execution-plan models shared by lifecycle surfaces.

The authored ChartLifecycle resource is the source of intent. These models
are the compiled boundary: commands can select and execute actions without
learning the shape of ``chart-lifecycle.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ActionKind(StrEnum):
    """Stable action vocabulary understood by executors and evidence stores."""

    HELM_DEPENDENCY_UPDATE = "helm-dependency-update"
    NAMESPACE_ENSURE = "namespace-ensure"
    HELM_LINT = "helm-lint"
    HELM_UPGRADE_INSTALL = "helm-upgrade-install"
    WORKLOAD_READY = "workload-ready"
    HELM_TEST = "helm-test"


@dataclass(frozen=True)
class ActionTarget:
    """Coordinates identifying the subject of one action."""

    chart: str
    profile: str | None = None
    environment: str | None = None
    release: str | None = None
    namespace: str | None = None


@dataclass(frozen=True)
class LifecycleAction:
    """One deterministic, immutable unit of lifecycle work."""

    action_id: str
    kind: ActionKind
    target: ActionTarget
    input_digest: str
    chart_path: Path
    values: tuple[Path, ...] = ()
    timeout: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LifecyclePlan:
    """A deterministic ordered action plan compiled from authored intent."""

    chart: str
    actions: tuple[LifecycleAction, ...]
    profile: str | None = None
    environment: str | None = None
    warnings: tuple[str, ...] = ()

    def action(self, action_id: str) -> LifecycleAction:
        """Return one action by ID."""
        for action in self.actions:
            if action.action_id == action_id:
                return action
        raise KeyError(action_id)
