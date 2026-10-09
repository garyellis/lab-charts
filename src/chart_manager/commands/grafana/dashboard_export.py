"""Export a Grafana dashboard and normalize it for git.

Replaces the older `scripts/export-grafana-dashboard.sh`. The normalization
rules match the shell script exactly so existing committed dashboards diff
cleanly against a fresh export.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError

DEFAULT_RELEASE = "grafana"
DEFAULT_ADMIN_USER = "admin"
SECRET_PASSWORD_KEY = "admin-password"

# Datasource UID rewrites: live UIDs from a running Grafana are replaced with
# the templated variable form so the JSON is portable across clusters.
_DATASOURCE_REWRITES = {
    "mimir": ("prometheus", "${DS_PROMETHEUS}"),
    "thanos": ("prometheus", "${DS_PROMETHEUS}"),
    "loki": ("loki", "${DS_LOKI}"),
    "tempo": ("tempo", "${DS_TEMPO}"),
}

# Top-level keys that Grafana increments on every save; stripping them keeps
# git diffs tied to real edits.
_CHURN_KEYS = ("id", "version", "iteration")


@dataclass(frozen=True)
class ExportRequest:
    """Inputs for one dashboard export: which dashboard, which Grafana, which release."""

    uid: str
    url: str
    namespace: str
    release: str = DEFAULT_RELEASE
    admin_user: str = DEFAULT_ADMIN_USER


def export(request: ExportRequest, kubectl: Kubectl) -> dict[str, Any]:
    """GET the dashboard from Grafana at `request.url` and return it normalized.

    Reads the admin password from the release's Secret. Raises
    ChartManagerError if the API response lacks a .dashboard object.
    `canonical_json` renders the result as the git-ready payload.
    """
    password = kubectl.get_secret_value(
        request.release, SECRET_PASSWORD_KEY, namespace=request.namespace
    )
    raw = _http_get_dashboard(request.url, request.uid, request.admin_user, password)

    dashboard = raw.get("dashboard")
    if not isinstance(dashboard, dict):
        raise ChartManagerError(
            f"Grafana API response has no .dashboard object for uid {request.uid!r}"
        )
    return normalize_dashboard(dashboard)


def canonical_json(dashboard: dict[str, Any]) -> str:
    """Serialize a dashboard the way it must appear on disk.

    Sorted keys, two-space indent, trailing newline. This is the git
    normalization contract: a committed dashboard and a fresh export of the
    same dashboard must be byte-identical, so every writer -- CLI, API,
    future bulk-export job -- has to go through this one function.
    """
    return json.dumps(dashboard, sort_keys=True, indent=2) + "\n"


@dataclass(frozen=True)
class DashboardSummary:
    """The few facts that identify one dashboard, for a human-readable report.

    A dashboard is a deep tree with no table form, so a surface asked for a
    human projection of one has to pick fields. Picking them here rather than
    in the CLI keeps the choice reviewable in one place and available to any
    other surface -- and keeps `cli/` from re-deriving `schemaVersion` and
    the templated datasource variables, which are the two things an export is
    normally checked for (see rules R003 and R007 in `dashboard_lint`).

    `top_level_panels` deliberately counts only the top level: panels nested
    inside a row are the row's contents, and a count that silently mixed the
    two would answer neither "how big is this board" nor "how many rows".
    """

    uid: str
    title: str
    schema_version: int | None
    top_level_panels: int
    datasource_variables: tuple[str, ...]


def summarize_dashboard(dashboard: dict[str, Any]) -> DashboardSummary:
    """Reduce a dashboard object to its identifying facts.

    Every field is optional in the input: this runs on whatever Grafana
    returned, and a dashboard missing `uid` or `schemaVersion` is a lint
    finding rather than a reason to fail an export.
    """
    schema_version = dashboard.get("schemaVersion")
    templating = (dashboard.get("templating") or {}).get("list") or []
    return DashboardSummary(
        uid=str(dashboard.get("uid") or ""),
        title=str(dashboard.get("title") or ""),
        schema_version=schema_version if isinstance(schema_version, int) else None,
        top_level_panels=len(dashboard.get("panels") or []),
        datasource_variables=tuple(
            str(var.get("name") or "")
            for var in templating
            if var.get("type") == "datasource"
        ),
    )


def normalize_dashboard(dashboard: dict[str, Any]) -> dict[str, Any]:
    """Strip churn, force editable, rewrite datasource UIDs to template form.

    Pure function.
    """
    out = {k: v for k, v in dashboard.items() if k not in _CHURN_KEYS}
    out["editable"] = True
    return _rewrite_datasource_uids(out)


def _rewrite_datasource_uids(node: Any) -> Any:
    """Recursively replace live datasource {type,uid} pairs with template-var form."""
    if isinstance(node, dict):
        uid = node.get("uid")
        type_ = node.get("type")
        if isinstance(uid, str) and isinstance(type_, str) and uid in _DATASOURCE_REWRITES:
            new_type, new_uid = _DATASOURCE_REWRITES[uid]
            return {"type": new_type, "uid": new_uid}
        return {k: _rewrite_datasource_uids(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_rewrite_datasource_uids(v) for v in node]
    return node


def _http_get_dashboard(base_url: str, uid: str, user: str, password: str) -> dict[str, Any]:
    """GET one dashboard from Grafana's API over basic auth; raise on HTTP/URL errors."""
    url = f"{base_url.rstrip('/')}/api/dashboards/uid/{uid}"
    creds = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        raise ChartManagerError(
            f"Grafana API GET {url} failed ({exc.code}): {body or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ChartManagerError(f"cannot reach Grafana at {url}: {exc.reason}") from exc
