"""Versioned wire contract for `helmrelease monitor` / `helmrelease test`.

This module is the single source of truth for the machine-readable shape of
monitor and test results. Every surface -- the CLI's `--output json`, a REST
endpoint, a Slack app, a CI step -- projects through `monitor_to_dict` /
`test_to_dict` so they cannot diverge while all claiming the same
`SCHEMA_VERSION`.

**Editing this module is a breaking change.** Adding a key is additive and
safe at the current version; renaming, removing, or retyping a key requires
bumping `SCHEMA_VERSION`.

Deliberately I/O-free and format-free: these functions return plain dicts.
They take no `file`, no `format=`, no `console=`. Choosing an encoder
(`json.dump` options, YAML, a HTTP response body) and performing the write is
the surface's job -- see `cli/helmrelease_render.py` for the CLI's encoder
settings.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .monitor import MonitorOutcome, MonitorResult
from .state import Transition
from .test import TestOutcome, TestResult

# Bump only on a breaking change to the payload shape; additive fields are
# safe at this version.
SCHEMA_VERSION = 1

__all__ = [
    "SCHEMA_VERSION",
    "monitor_to_dict",
    "test_to_dict",
]


def monitor_to_dict(
    result: MonitorResult,
    *,
    chart: str,
    version: str,
) -> dict[str, Any]:
    """Project a MonitorResult onto the versioned wire payload."""
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "monitor",
        "chart": chart,
        "version": version,
        "ok": result.ok,
        "total_timed_out": result.total_timed_out,
        "duration_seconds": result.total_duration_seconds,
        "outcomes": [_serialize_monitor_outcome(o) for o in result.outcomes],
    }


def test_to_dict(
    result: TestResult,
    *,
    chart: str,
    version: str,
) -> dict[str, Any]:
    """Project a TestResult onto the versioned wire payload."""
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "test",
        "chart": chart,
        "version": version,
        "ok": result.ok,
        "total_timed_out": result.total_timed_out,
        "duration_seconds": result.total_duration_seconds,
        "outcomes": [_serialize_test_outcome(o) for o in result.outcomes],
    }


def _serialize_condition(c: Any) -> dict[str, Any]:
    """JSON-serialize a HelmRelease status condition."""
    return {
        "type": c.type,
        "status": c.status,
        "reason": c.reason,
        "message": c.message,
        "last_transition_time": (
            c.last_transition_time.isoformat() if c.last_transition_time else None
        ),
    }


def _serialize_transition(t: Transition) -> dict[str, Any]:
    """JSON-serialize a progress Transition."""
    return {
        "at": t.at.isoformat() if isinstance(t.at, datetime) else str(t.at),
        "phase": t.phase,
        "detail": t.detail,
    }


def _serialize_workload_rollout(w: Any) -> dict[str, Any]:
    """JSON-serialize a workload rollout snapshot (flattens w.workload)."""
    wl = w.workload
    return {
        "kind": wl.kind,
        "namespace": wl.namespace,
        "name": wl.name,
        "desired": wl.desired,
        "ready": wl.ready,
        "available": wl.available,
        "generation": w.generation,
        "observed_generation": w.observed_generation,
        "converged": w.converged,
    }


def _serialize_monitor_outcome(o: MonitorOutcome) -> dict[str, Any]:
    """JSON-serialize one per-HelmRelease monitor outcome."""
    status = o.last_status
    return {
        "namespace": o.ref.namespace,
        "name": o.ref.name,
        "verdict": o.verdict,
        "reason": o.reason,
        "duration_seconds": o.duration_seconds,
        "conditions": (
            [_serialize_condition(c) for c in status.conditions] if status else []
        ),
        "observed_generation": status.observed_generation if status else None,
        "generation": status.generation if status else None,
        "history_chart_version": status.history_chart_version if status else None,
        "workloads": [_serialize_workload_rollout(w) for w in o.last_workloads],
        "recent_transitions": [_serialize_transition(t) for t in o.recent_transitions],
        "diagnostics": o.diagnostics,
    }


def _serialize_test_outcome(o: TestOutcome) -> dict[str, Any]:
    """JSON-serialize one per-HelmRelease test outcome."""
    return {
        "namespace": o.ref.namespace,
        "name": o.ref.name,
        "verdict": o.verdict,
        "reason": o.reason,
        "duration_seconds": o.duration_seconds,
        "helm_test_returncode": o.helm_test_returncode,
        "helm_test_stdout": o.helm_test_stdout,
        "helm_test_stderr": o.helm_test_stderr,
        "test_pods": [
            {
                "namespace": p.namespace,
                "name": p.name,
                "phase": p.phase,
                "logs": p.logs,
                "previous_logs": p.previous_logs,
            }
            for p in o.test_pods
        ],
        "phase_log": [_serialize_transition(t) for t in o.phase_log],
        "diagnostics": o.diagnostics,
    }
