"""The JSON/YAML shape of `local up`, `down`, `reset`, `status` and their `--dry-run` plans.

Access hints are left out of `converge_to_dict` on purpose: they can carry credentials,
which do not belong in a document piped to a file. `status_to_dict` carries URLs only.
"""

from __future__ import annotations

from typing import Any

from .models import (
    DevClusterActionResult,
    DevClusterEntryOutcome,
    DevClusterPlan,
    DevClusterResult,
    DevClusterStatus,
)

__all__ = [
    "action_to_dict",
    "converge_to_dict",
    "plan_to_dict",
    "status_to_dict",
]


def converge_to_dict(
    result: DevClusterResult,
    *,
    command: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Project an `up` / `reset` run onto the wire payload.

    `command` and `cluster_name` echo the request: the result carries the
    outcome but not which verb produced it or which cluster it landed on,
    and a caller reading this off stdout has no other handle on either.

    The three buckets stay separate rather than collapsing into one list
    with a `status` key. They are the vocabulary `local` reports in and
    the table renders, and flattening them here would make the payload and
    the table two different accounts of one run.
    """
    return {
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
    result: DevClusterActionResult,
    *,
    command: str,
) -> dict[str, Any]:
    """Project a `down` onto the wire payload.

    `changed` is the whole answer: `ok` is unconditionally true because a
    cluster that was already stopped is a success, and a caller that needs
    to know whether this invocation is what stopped it reads `changed`.
    """
    return {
        "command": command,
        "cluster_name": result.cluster_name,
        "ok": result.ok,
        "changed": result.changed,
    }


def status_to_dict(status: DevClusterStatus) -> dict[str, Any]:
    """Project a cluster snapshot onto the wire payload.

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
        "drift": {
            "missing_host_ports": list(status.drift.missing),
            "error": status.drift.error,
        },
    }


def plan_to_dict(plan: DevClusterPlan) -> dict[str, Any]:
    """Project a `--dry-run` plan onto the wire payload.

    `dry_run: true` is a key rather than an inference from `command`,
    because the same `command` value appears on the payload a real run
    emits. A consumer that mistook one for the other would report a
    converge that never happened.
    """
    return {
        "command": plan.command,
        "dry_run": True,
        "cluster_name": plan.cluster_name,
        "ok": True,
        "target": plan.target,
        "target_kind": plan.target_kind,
        "destroys": plan.destroys,
        "provisioning_hooks_enabled": plan.provisioning_hooks_enabled,
        "provisioning_hooks": [
            {"phase": phase, "argv": list(argv)} for phase, argv in plan.provisioning_hooks
        ],
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


def _entry(entry: DevClusterEntryOutcome) -> dict[str, Any]:
    """JSON-serialize one converged plan entry."""
    return {
        "chart": entry.chart,
        "profile": entry.profile,
        "namespace": entry.namespace,
    }
