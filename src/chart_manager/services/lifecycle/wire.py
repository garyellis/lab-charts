"""Wire contract for compiled lifecycle plans and change impact.

This module is the single source of truth for the machine-readable shape of
the two documents `services/lifecycle/` produces: the action plan a compiler
emits (`chart test --dry-run`) and the change-impact selection CI reads
(`plan -o json|yaml`, `ci impact`). Every surface -- the CLI's `-o json`, a
REST endpoint, a Slack app, a CI step -- projects through `plan_to_dict` /
`impact_to_dict` so they cannot diverge.

Both projections emit a stable key set rather than omitting empty ones, so
`jq '.actions[].timeout'` works on every run.

Deliberately I/O-free and format-free: these functions return plain,
JSON-ready dicts. They take no `file`, no `format=`, no `console=`. Choosing
an encoder and rendering for humans is the surface's job -- see
`cli/chart.py` and `cli/plan.py`.
"""

from __future__ import annotations

from typing import Any

from chart_manager.services.lifecycle.impact import (
    ClusterTestImpact,
    ImpactReason,
    LifecycleImpact,
    ValidationImpact,
)
from chart_manager.services.lifecycle.models import (
    ActionTarget,
    LifecycleAction,
    LifecyclePlan,
)

__all__ = [
    "impact_to_dict",
    "plan_to_dict",
]


def plan_to_dict(plan: LifecyclePlan) -> dict[str, Any]:
    """Project a compiled `LifecyclePlan` onto the wire payload."""
    return {
        "chart": plan.chart,
        "profile": plan.profile,
        "environment": plan.environment,
        "actions": [_action(action) for action in plan.actions],
        "warnings": list(plan.warnings),
    }


def impact_to_dict(impact: LifecycleImpact) -> dict[str, Any]:
    """Project a `LifecycleImpact` onto the wire payload.

    `spec_errors` is carried in the document rather than replacing it: the
    selection derived from the files that *did* parse is still the answer to
    the question asked, and the CLI exits non-zero off the same list -- see
    `cli/plan.py` for why that is a spec exit rather than a generic failure.
    """
    return {
        "changed_files": [path.as_posix() for path in impact.changed_files],
        "validation_selection": [_validation_case(case) for case in impact.validation],
        "cluster_test_matrix": [_cluster_test_case(case) for case in impact.cluster_tests],
        "spec_errors": list(impact.spec_errors),
        "warnings": list(impact.warnings),
    }


def _action(action: LifecycleAction) -> dict[str, Any]:
    """JSON-serialize one planned action in compiled execution order."""
    return {
        "action_id": action.action_id,
        "kind": action.kind.value,
        "target": _target(action.target),
        # The digest is what makes a plan comparable across two compilations
        # of the same inputs; it is the key a consumer caches or diffs on.
        "input_digest": action.input_digest,
        "chart_path": action.chart_path.as_posix(),
        "values": [path.as_posix() for path in action.values],
        "timeout": action.timeout,
        "metadata": dict(action.metadata),
        "command": list(action.command),
    }


def _target(target: ActionTarget) -> dict[str, Any]:
    """JSON-serialize the coordinates one action acts on."""
    return {
        "chart": target.chart,
        "profile": target.profile,
        "environment": target.environment,
        "release": target.release,
        "namespace": target.namespace,
    }


def _validation_case(case: ValidationImpact) -> dict[str, Any]:
    """JSON-serialize one selected chart/environment validation case."""
    return {
        "chart": case.chart,
        "environment": case.environment,
        "release": case.release,
        "namespace": case.namespace,
        "reasons": [_reason(reason) for reason in case.reasons],
    }


def _cluster_test_case(case: ClusterTestImpact) -> dict[str, Any]:
    """JSON-serialize one selected chart/profile live-cluster matrix entry."""
    return {
        "chart": case.chart,
        "profile": case.profile,
        "reasons": [_reason(reason) for reason in case.reasons],
    }


def _reason(reason: ImpactReason) -> dict[str, Any]:
    """JSON-serialize one changed file and the rule that selected a case."""
    return {
        "code": reason.code.value,
        "changed_file": reason.changed_file.as_posix(),
        "detail": reason.detail,
    }
