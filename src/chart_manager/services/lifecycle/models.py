"""Immutable execution-plan models shared by lifecycle surfaces.

The authored ChartLifecycle resource is the source of intent. These models
are the compiled boundary: commands can select and execute actions without
learning the shape of ``chart-lifecycle.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

LIFECYCLE_API_VERSION = "lifecycle.chartmanager.io/v1alpha1"


class Workflow(StrEnum):
    """Lifecycle workflows compiled from authored ChartLifecycle resources."""

    VALIDATION = "validation"
    CLUSTER_TEST = "cluster-test"


class ActionKind(StrEnum):
    """Stable action vocabulary understood by executors and evidence stores."""

    HELM_DEPENDENCY_UPDATE = "helm-dependency-update"
    RENDER = "render"
    SCHEMA_VALIDATE = "schema-validate"
    POLICY_VALIDATE = "policy-validate"
    NAMESPACE_ENSURE = "namespace-ensure"
    HELM_LINT = "helm-lint"
    HELM_UPGRADE_INSTALL = "helm-upgrade-install"
    WORKLOAD_READY = "workload-ready"
    HELM_TEST = "helm-test"


@dataclass(frozen=True)
class ActionTarget:
    """Coordinates identifying the subject of one action."""

    workflow: Workflow
    chart: str
    profile: str | None = None
    environment: str | None = None
    release: str | None = None
    namespace: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Return a compact JSON-safe coordinate projection."""
        return {
            key: value
            for key, value in (
                ("workflow", self.workflow.value),
                ("chart", self.chart),
                ("profile", self.profile),
                ("environment", self.environment),
                ("release", self.release),
                ("namespace", self.namespace),
            )
            if value is not None
        }


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

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-serializable action projection."""
        result: dict[str, Any] = {
            "actionId": self.action_id,
            "kind": self.kind.value,
            "target": self.target.to_dict(),
            "inputDigest": self.input_digest,
            "chartPath": str(self.chart_path),
        }
        if self.values:
            result["values"] = [str(path) for path in self.values]
        if self.timeout is not None:
            result["timeout"] = self.timeout
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result


@dataclass(frozen=True)
class LifecyclePlan:
    """A deterministic ordered action plan compiled from authored intent."""

    workflow: Workflow
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

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON projection used by CLI and evidence layers."""
        result: dict[str, Any] = {
            "apiVersion": LIFECYCLE_API_VERSION,
            "kind": "LifecyclePlan",
            "workflow": self.workflow.value,
            "chart": self.chart,
            "actions": [action.to_dict() for action in self.actions],
        }
        if self.profile is not None:
            result["profile"] = self.profile
        if self.environment is not None:
            result["environment"] = self.environment
        if self.warnings:
            result["warnings"] = list(self.warnings)
        return result

