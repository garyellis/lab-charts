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

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .monitor import MonitorOutcome, MonitorResult
from .promote import PromoteResult
from .scanner import HelmReleaseMatch
from .state import PromoteStatus, Transition
from .test import TestOutcome, TestResult

# Bump only on a breaking change to the payload shape; additive fields are
# safe at this version.
SCHEMA_VERSION = 1

#: promote terminal state -> process exit code, per the exit-code table in
#: `docs/plans/2026-07-30-cli-surface-and-events-refactor.md` §6.1:
#:
#:   0 = success · 1 = "the thing you asked about failed"
#:
#: It lives here, beside the payload, because the payload's `ok` field and the
#: CLI's exit status are the same judgement expressed twice. Splitting them
#: is how `promote` shipped a state (`ABORTED`) that printed a failure and
#: exited 0.
#:
#: Why each arm:
#:   PR_OPENED / PUSHED  -- the PR exists; that is the whole request.
#:   ALREADY_OPEN        -- idempotent re-run; the requested PR is open and
#:                          `PROMOTE_PHASE` records it as a real forward
#:                          transition (AWAITING_MERGE), not a failure.
#:   NO_CHANGES          -- every match is already at the target version, so
#:                          the desired state holds. A promote must be safe to
#:                          re-run in CI.
#:   DRY_RUN             -- §6.3: a dry run prints the plan and exits 0.
#:   ABORTED             -- §6.1 names this verbatim: "promote aborted/declined"
#:                          is an exit-1 case. Nothing was promoted.
#:
#: Deliberately no code 2 here: 2 is reserved for usage errors, which the
#: surface raises during argument handling and which never reach a
#: `PromoteResult`. Tool error stays on its current code until P2.
PROMOTE_EXIT_CODE: Mapping[PromoteStatus, int] = {
    PromoteStatus.NO_CHANGES: 0,
    PromoteStatus.DRY_RUN: 0,
    PromoteStatus.ALREADY_OPEN: 0,
    PromoteStatus.PR_OPENED: 0,
    PromoteStatus.PUSHED: 0,
    PromoteStatus.ABORTED: 1,
}

__all__ = [
    "PROMOTE_EXIT_CODE",
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

    `ok` is `PROMOTE_EXIT_CODE[status] == 0`, never an independent predicate,
    so a consumer branching on `.ok` and a shell branching on `$?` can never
    disagree.
    """
    pr = result.pull_request
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "promote",
        "chart": chart,
        "version": version,
        "environment": environment,
        "path": str(path),
        "ok": PROMOTE_EXIT_CODE[result.status] == 0,
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
