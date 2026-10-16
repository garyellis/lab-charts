"""The document for a compiled lifecycle plan (`chart test --dry-run -o json`).

Emits a stable key set rather than omitting empty ones, so
`jq '.actions[].timeout'` works on every run.
"""

from __future__ import annotations

from typing import Any

from chart_manager.commands.test.models import LifecycleAction, LifecyclePlan
from chart_manager.plumbing.commands import redact

__all__ = ["plan_to_dict"]


def plan_to_dict(plan: LifecyclePlan) -> dict[str, Any]:
    """Project a compiled `LifecyclePlan` onto its document."""
    return {
        "chart": plan.chart,
        "profile": plan.profile,
        "actions": [_action(action) for action in plan.actions],
        "warnings": list(plan.warnings),
    }


def _action(action: LifecycleAction) -> dict[str, Any]:
    """One planned action; a hook's command is masked as the table masks it."""
    entry = action.entry
    return {
        "action_id": action.action_id,
        "kind": action.kind.value,
        "chart": entry.chart.name,
        "profile": entry.profile,
        "namespace": entry.namespace,
        "chart_path": entry.chart.path.as_posix(),
        "values": [path.as_posix() for path in entry.values],
        "timeout": entry.spec.timeout,
        "command": redact(action.command),
    }
