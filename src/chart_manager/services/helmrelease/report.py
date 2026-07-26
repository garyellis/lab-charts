"""The fragments of a failure report that monitor and test must render alike.

Deliberately small. `MonitorService` and `TestService` produce genuinely
different reports past the first two sections -- one lists workload rollouts
and their events, the other lists test pods and their logs -- and folding
those into one parameterised builder would trade a little duplication for a
function with two disjoint halves and a flag to pick between them.

What *is* shared is the part an operator uses to orient: the heading that
names the release and the verdict, and the condition table underneath it.
Those two had drifted only in their condition tuples, and the events
placeholder had drifted outright -- the same failure rendered as
`Events: (unavailable: ...)` in one report and `<events unavailable: ...>`
in the other, which is the kind of difference that makes a grep across
CI logs quietly miss half the failures.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from chart_manager.integrations.helmrelease import HelmReleaseRef, HelmReleaseStatus
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.text import truncate_lines
from chart_manager.services.helmrelease.state import DETAIL_MAX, ReasonLike, Verdict

__all__ = ["EVENTS_LINE_CAP", "conditions", "header", "safe_events"]

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


def safe_events(fetch: Callable[[], str]) -> str:
    """Run `fetch`, returning a placeholder line instead of raising.

    Events are supporting evidence. A cluster that has become unreachable
    while we were composing a failure report must not replace the report --
    the verdict we already have is the thing the caller asked for.
    """
    try:
        blob = fetch()
    except ExternalCommandError as exc:
        stderr = (exc.stderr or str(exc)).strip()
        return f"<events unavailable: {stderr[:DETAIL_MAX]}>"
    return truncate_lines(blob, EVENTS_LINE_CAP)
