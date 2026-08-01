"""Versioned wire contract for the `chart-manager local` command group.

Single source of truth for the machine-readable shape of `up`, `down`,
`reset`, `status`, and the `--dry-run` plan any of the three mutating
commands prints. Every surface -- the CLI's `-o json`/`-o yaml`, a REST
endpoint, a CI step -- projects through these functions, so they cannot
diverge while all claiming the same `SCHEMA_VERSION`.

**Editing this module is a breaking change.** Adding a key is additive and
safe at the current version; renaming, removing, or retyping a key requires
bumping `SCHEMA_VERSION`.

Deliberately I/O-free and format-free, like `services/helmrelease/wire.py`:
these return plain dicts and take no `file`, no `format=`, no `console=`.
Picking an encoder and writing it is the surface's job.

One thing is deliberately *absent* from `converge_to_dict`: the access hints
(`urls`, the CA-trust advice, the Grafana credentials). They are advice for
an operator about how to reach what was just installed, they are printed on
stderr for exactly that reason (`cli/local._render_access_hints`), and a
credential does not belong in a document a caller pipes into a file. The
document version of "what can I reach" is `status_to_dict`, where it is the
answer to the question rather than a footnote on a mutation -- and where it
carries URLs only.
"""

from __future__ import annotations

from typing import Any

from .models import (
    DevelopmentClusterActionResult,
    DevelopmentClusterEntryOutcome,
    DevelopmentClusterPlan,
    DevelopmentClusterResult,
    DevelopmentClusterStatus,
)

# Bump only on a breaking change to the payload shape; additive fields are
# safe at this version.
SCHEMA_VERSION = 1

__all__ = [
    "SCHEMA_VERSION",
    "action_to_dict",
    "converge_to_dict",
    "plan_to_dict",
    "status_to_dict",
]


def converge_to_dict(
    result: DevelopmentClusterResult,
    *,
    command: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Project an `up` / `reset` run onto the versioned wire payload.

    `command` and `cluster_name` echo the request: the result carries the
    outcome but not which verb produced it or which cluster it landed on,
    and a caller reading this off stdout has no other handle on either.

    The three buckets stay separate rather than collapsing into one list
    with a `status` key. They are the vocabulary the service reports in and
    the table renders, and flattening them here would make the payload and
    the table two different accounts of one run.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "cluster_name": cluster_name,
        "ok": result.ok,
        "applied": [_entry(e) for e in result.applied],
        "no_change": [_entry(e) for e in result.no_change],
        "failed": [
            {
                "chart": failure.chart,
                "profile": failure.profile,
                "namespace": failure.namespace,
                "error": failure.error,
            }
            for failure in result.failed
        ],
    }


def action_to_dict(
    result: DevelopmentClusterActionResult,
    *,
    command: str,
) -> dict[str, Any]:
    """Project a `down` onto the versioned wire payload.

    `changed` is the whole answer: `ok` is unconditionally true because a
    cluster that was already stopped is a success, and a caller that needs
    to know whether this invocation is what stopped it reads `changed`.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "cluster_name": result.cluster_name,
        "ok": result.ok,
        "changed": result.changed,
        "port_forward_pid": result.port_forward_pid,
    }


def status_to_dict(status: DevelopmentClusterStatus) -> dict[str, Any]:
    """Project a cluster snapshot onto the versioned wire payload.

    `ok` is `exists`, not "everything is healthy". `local status` reports;
    it does not grade. A release stuck in `pending-upgrade` is what the
    caller reads `releases[].status` for -- the documented idiom is
    `local status -o json | jq '.releases[] | select(.status!="deployed")'`
    -- and having `ok` pre-empt that judgement would make the payload and
    the caller's filter two different opinions about the same cluster.

    Every `*_error` key is `null` on a clean lookup and a string on a failed
    one. An empty list beside a non-null error means "could not tell", which
    is not the same answer as an empty list beside `null`.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "status",
        "cluster_name": status.cluster_name,
        "ok": status.exists,
        "exists": status.exists,
        "context": status.context,
        "provider": status.provider,
        "releases": [
            {
                "name": release.name,
                "namespace": release.namespace,
                "revision": release.revision,
                "status": release.status,
            }
            for release in status.releases
        ],
        "releases_error": status.releases_error,
        "urls": list(status.urls),
        "urls_error": status.urls_error,
        "port_forward_pid": status.port_forward_pid,
        "drift": {
            "missing_host_ports": list(status.drift.missing),
            "error": status.drift.error,
        },
    }


def plan_to_dict(plan: DevelopmentClusterPlan) -> dict[str, Any]:
    """Project a `--dry-run` plan onto the versioned wire payload.

    `dry_run: true` is a key rather than an inference from `command`,
    because the same `command` value appears on the payload a real run
    emits. A consumer that mistook one for the other would report a
    converge that never happened.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "command": plan.command,
        "dry_run": True,
        "cluster_name": plan.cluster_name,
        "ok": True,
        "target": plan.target,
        "target_kind": plan.target_kind,
        "destroys": plan.destroys,
        "entries": [
            {
                "chart": entry.chart,
                "profile": entry.profile,
                "namespace": entry.namespace,
                "source": entry.source,
            }
            for entry in plan.entries
        ],
    }


def _entry(entry: DevelopmentClusterEntryOutcome) -> dict[str, Any]:
    """JSON-serialize one converged plan entry."""
    return {
        "chart": entry.chart,
        "profile": entry.profile,
        "namespace": entry.namespace,
    }
