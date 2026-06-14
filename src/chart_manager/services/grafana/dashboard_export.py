"""Export a Grafana dashboard from a kind cluster and normalize for git.

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
DEFAULT_REMOTE_PORT = 80
DEFAULT_ADMIN_USER = "admin"
SECRET_PASSWORD_KEY = "admin-password"

# Datasource UID rewrites: live UIDs from a running Grafana are replaced with
# the templated variable form so the JSON is portable across clusters.
_DATASOURCE_REWRITES = {
    "mimir": ("prometheus", "${DS_PROMETHEUS}"),
    "loki": ("loki", "${DS_LOKI}"),
    "tempo": ("tempo", "${DS_TEMPO}"),
}

# Top-level keys that Grafana increments on every save; stripping them keeps
# git diffs tied to real edits.
_CHURN_KEYS = ("id", "version", "iteration")


@dataclass(frozen=True)
class ExportRequest:
    uid: str
    cluster_name: str
    namespace: str
    release: str = DEFAULT_RELEASE
    remote_port: int = DEFAULT_REMOTE_PORT
    admin_user: str = DEFAULT_ADMIN_USER


class GrafanaExporter:
    def __init__(self, *, kubectl: Kubectl | None = None) -> None:
        self.kubectl = kubectl or Kubectl()

    def fetch(self, request: ExportRequest) -> dict[str, Any]:
        password = self.kubectl.get_secret_value(
            request.release, SECRET_PASSWORD_KEY, namespace=request.namespace
        )
        context = f"kind-{request.cluster_name}"

        with self.kubectl.port_forward_session(
            context=context,
            namespace=request.namespace,
            service=request.release,
            remote_port=request.remote_port,
        ) as local_port:
            raw = _http_get_dashboard(
                local_port, request.uid, request.admin_user, password
            )

        dashboard = raw.get("dashboard")
        if not isinstance(dashboard, dict):
            raise ChartManagerError(
                f"Grafana API response has no .dashboard object for uid {request.uid!r}"
            )
        return normalize_dashboard(dashboard)


def normalize_dashboard(dashboard: dict[str, Any]) -> dict[str, Any]:
    """Strip churn, force editable, rewrite datasource UIDs to template form.

    Pure function -- equivalent to the jq pipeline in the legacy shell script.
    """
    out = {k: v for k, v in dashboard.items() if k not in _CHURN_KEYS}
    out["editable"] = True
    return _rewrite_datasource_uids(out)


def _rewrite_datasource_uids(node: Any) -> Any:
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


def _http_get_dashboard(
    local_port: int, uid: str, user: str, password: str
) -> dict[str, Any]:
    url = f"http://127.0.0.1:{local_port}/api/dashboards/uid/{uid}"
    creds = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
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
