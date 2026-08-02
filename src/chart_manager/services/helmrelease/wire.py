"""Versioned wire contract for `helmrelease monitor` / `test` / `promote`.

This module is the single source of truth for the machine-readable shape of
monitor, test, and promote results. Every surface -- the CLI's `--output
json`, a REST endpoint, a Slack app, a CI step -- projects through
`monitor_to_dict` / `test_to_dict` / `promote_to_dict` so they cannot diverge
while all claiming the same `SCHEMA_VERSION`.

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
from pathlib import Path
from typing import Any

from chart_manager.plumbing.exit_codes import Outcome

from .helm_test import TestOutcome, TestResult
from .monitor import MonitorOutcome, MonitorResult
from .promote import PromoteResult
from .scanner import HelmReleaseMatch
from .state import PROMOTE_OUTCOME, Transition

# Bump only on a breaking change to the payload shape; additive fields are
# safe at this version.
SCHEMA_VERSION = 1

__all__ = [
    "SCHEMA_VERSION",
    "monitor_to_dict",
    "promote_to_dict",
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


def promote_to_dict(
    result: PromoteResult,
    *,
    chart: str,
    version: str,
    environment: str,
    path: Path,
) -> dict[str, Any]:
    """Project a PromoteResult onto the versioned wire payload.

    `chart` / `version` / `environment` / `path` echo the request: a
    `PromoteResult` carries the outcome but not what was asked for, and a CI
    step reading this off stdout has no other handle on which promotion it is
    looking at.

    `ok` is `PROMOTE_OUTCOME[status] is Outcome.SUCCESS`, never an
    independent predicate. The CLI exits with `exit_code_for` of that same
    lookup, so a consumer branching on `.ok` and a shell branching on `$?`
    read one judgement made in one place -- see
    `plumbing/exit_codes.py` for why the number itself is not decided here.
    """
    pr = result.pull_request
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "promote",
        "chart": chart,
        "version": version,
        "environment": environment,
        "path": str(path),
        "ok": PROMOTE_OUTCOME[result.status] is Outcome.SUCCESS,
        "status": str(result.status),
        "branch": result.branch,
        "pull_request": (
            {"url": pr.url, "number": pr.number, "branch": pr.branch}
            if pr is not None
            else None
        ),
        # Paths as the service produced them. They sit under the promote run's
        # temporary clone, so they are evidence of *which files* changed, not
        # locations a consumer can open after the process exits.
        "changed_files": [str(p) for p in result.changed_files],
        "matches": [_serialize_match(m) for m in result.matches],
        "downgrades": [_serialize_match(m) for m in result.downgrades],
    }


def _serialize_match(m: HelmReleaseMatch) -> dict[str, Any]:
    """JSON-serialize one scanned HelmRelease document."""
    return {
        "path": str(m.path),
        "doc_index": m.doc_index,
        "name": m.name,
        "namespace": m.namespace,
        "current_version": m.current_version,
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
