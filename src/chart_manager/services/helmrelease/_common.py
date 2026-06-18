"""Shared types and helpers for the helmrelease service layer.

Lives here (not in monitor.py) so test.py and any future helmrelease
subservice can import Transition, the no-match sentinel, the matched-
status filter, and the truncation helpers without cross-importing
sibling services. Keeps the public surface in `__init__` stable.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from chart_manager.integrations.flux import Flux, HelmReleaseRef, HelmReleaseStatus


@dataclass(frozen=True)
class Transition:
    at: datetime
    phase: str
    detail: str


ProgressCallback = Callable[[HelmReleaseRef, Transition], None] | None


# Synthetic ref used to surface the zero-match case as a regular outcome
# rather than an empty result, so callers render it the same way as
# real failures.
NO_MATCH_REF = HelmReleaseRef(
    name="<no-match>",
    namespace="",
    api_version="",
    release_name="",
    storage_namespace="",
    target_namespace="",
)


def filter_matched_statuses(
    flux: Flux,
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
    refs = flux.list(namespace=namespace, timeout=per_poll)
    matched: list[HelmReleaseStatus] = []
    for ref in refs:
        if namespace is not None and ref.namespace != namespace:
            continue
        status = flux.get_status(ref, timeout=per_poll)
        if (
            status.desired_chart_name == chart_name
            and status.desired_chart_version == version
        ):
            matched.append(status)
    return matched


def truncate_lines(blob: str, max_lines: int) -> str:
    if not blob:
        return ""
    lines = blob.splitlines()
    if len(lines) <= max_lines:
        return blob.rstrip("\n")
    head = lines[:max_lines]
    head.append(f"... ({len(lines) - max_lines} more line(s) truncated)")
    return "\n".join(head)


def truncate_bytes(blob: str, max_bytes: int) -> str:
    if not blob:
        return ""
    encoded = blob.encode("utf-8")
    if len(encoded) <= max_bytes:
        return blob
    # Decode with errors="ignore" so we never split a multibyte codepoint
    # mid-sequence and emit an invalid utf-8 boundary.
    head = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{head}\n... ({len(encoded) - max_bytes} more byte(s) truncated)"
