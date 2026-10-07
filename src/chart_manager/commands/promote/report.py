"""The fragments of a failure report that monitor and test must render alike.

Only the heading and the condition table are shared; past those, the two
reports differ (workload rollouts and events versus test pods and logs).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from chart_manager.commands.promote.state import DETAIL_MAX, ReasonLike, Verdict
from chart_manager.integrations.kubectl import HelmReleaseRef, HelmReleaseStatus
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.text import truncate_lines

__all__ = ["EVENTS_LINE_CAP", "conditions", "failure_detail", "header", "safe_events"]

#: `kubectl get events` output is unbounded and mostly repetition; the tail is
#: what explains a failure, but the head is what fits in a report.
EVENTS_LINE_CAP = 80


def header(ref: HelmReleaseRef, verdict: Verdict, reason: ReasonLike) -> str:
    """Render the `## ns/name - verdict: reason` heading a report opens with."""
    ns = ref.namespace or "(none)"
    name = ref.name or "(none)"
    return f"## {ns}/{name} - {verdict}: {reason}"


def conditions(status: HelmReleaseStatus, cond_types: Iterable[str]) -> list[str]:
    """Render the `### Status` block for `cond_types`, in the order given.

    Absent conditions are printed rather than skipped: "TestSuccess: (absent)"
    and "no TestSuccess row" look identical in a report but mean different
    things -- the chart has no test hook versus the report is incomplete.
    """
    lines = ["\n### Status"]
    for cond_type in cond_types:
        cond = status.condition(cond_type)
        if cond is None:
            lines.append(f"- {cond_type}: (absent)")
        else:
            lines.append(f"- {cond_type}: {cond.status} ({cond.reason}) - {cond.message}")
    return lines


def failure_detail(exc: ExternalCommandError) -> str:
    """Render `exc` as the one capped line a report bullet has room for.

    Keeps the stderr, so RBAC-denied, apiserver-unreachable and finalizer-stuck
    failures render differently.
    """
    stderr = (exc.stderr or str(exc)).strip()
    return stderr[:DETAIL_MAX]


def safe_events(fetch: Callable[[], str]) -> str:
    """Run `fetch`, returning a placeholder line instead of raising.

    An unreachable cluster must not replace a report whose verdict we already have.
    """
    try:
        blob = fetch()
    except ExternalCommandError as exc:
        return f"<events unavailable: {failure_detail(exc)}>"
    return truncate_lines(blob, EVENTS_LINE_CAP)
