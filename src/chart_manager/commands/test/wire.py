"""Wire contract for a compiled lifecycle plan (`chart test --dry-run -o json`).

Emits a stable key set rather than omitting empty ones, so
`jq '.actions[].timeout'` works on every run.
"""

from __future__ import annotations

from typing import Any

from chart_manager.commands.test.models import (
    ActionTarget,
    LifecycleAction,
    LifecyclePlan,
)

__all__ = ["plan_to_dict"]


def plan_to_dict(plan: LifecyclePlan) -> dict[str, Any]:
    """Project a compiled `LifecyclePlan` onto the wire payload."""
    return {
        "chart": plan.chart,
        "profile": plan.profile,
        "actions": [_action(action) for action in plan.actions],
        "warnings": list(plan.warnings),
    }


def _action(action: LifecycleAction) -> dict[str, Any]:
    """JSON-serialize one planned action in compiled execution order."""
    return {
        "action_id": action.action_id,
        "kind": action.kind.value,
        "target": _target(action.target),
        "chart_path": action.chart_path.as_posix(),
        "values": [path.as_posix() for path in action.values],
        "timeout": action.timeout,
        "command": list(action.command),
    }


def _target(target: ActionTarget) -> dict[str, Any]:
    """JSON-serialize the coordinates one action acts on."""
    return {
        "chart": target.chart,
        "profile": target.profile,
        "release": target.release,
        "namespace": target.namespace,
    }
