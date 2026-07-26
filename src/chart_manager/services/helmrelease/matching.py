"""Selecting which HelmReleases a monitor/test run acts on.

Its own module, not a "helper", because this is network I/O: one
`kubectl get hr` plus one `kubectl get hr/<name>` per ref, all serial and
all before any thread pool exists. Filed next to `monitor.py`/`test.py` so
that cost is visible to whoever changes the fan-out, and so the known N+1
(see discovery F12a) has one place to be fixed rather than two.
"""
from __future__ import annotations

from chart_manager.integrations.helmrelease import HelmReleaseClient, HelmReleaseStatus

__all__ = ["filter_matched_statuses"]


def filter_matched_statuses(
    client: HelmReleaseClient,
    *,
    namespace: str | None,
    chart_name: str,
    version: str,
    per_poll: float,
) -> list[HelmReleaseStatus]:
    """List Flux HelmReleases and return statuses matching chart_name@version.

    Issues one `kubectl get hr` (scoped via `namespace`) and one
    `kubectl get hr/<name>` per ref. Filtering by (desired_chart_name,
    desired_chart_version) here keeps each subservice's fan-out targeting
    consistent.
    """
    refs = client.list(namespace=namespace, timeout=per_poll)
    matched: list[HelmReleaseStatus] = []
    for ref in refs:
        if namespace is not None and ref.namespace != namespace:
            continue
        status = client.get_status(ref, timeout=per_poll)
        if (
            status.desired_chart_name == chart_name
            and status.desired_chart_version == version
        ):
            matched.append(status)
    return matched
