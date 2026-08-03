"""Versioned wire contract for `event list`.

This module is the single source of truth for the machine-readable shape of
an event listing. Every surface -- the CLI's `-o json|yaml`, a REST endpoint,
a Slack app -- projects through `events_to_dict` so they cannot diverge while
all claiming the same `SCHEMA_VERSION`.

**Editing this module is a breaking change.** Adding a key is additive and
safe at the current version; renaming, removing, or retyping a key requires
bumping `SCHEMA_VERSION`.

The `events` entries are the stored ledger documents themselves (the shape
`PlatformLifecycleEvent.to_dict` writes), passed through rather than
re-projected: the ledger *is* the contract for one event, and a second
projection here would be a second place its field names live. The envelope
adds the selection that produced the page, so a consumer can tell a filtered
answer from an unfiltered one without re-deriving it from the rows.

Deliberately I/O-free and format-free: this returns a plain, JSON-ready
dict. Choosing an encoder and rendering for humans is the surface's job --
see `cli/events.py`.
"""

from __future__ import annotations

from typing import Any

from chart_manager.services.events.query import EventQuery

# Bump only on a breaking change to the payload shape; additive fields are
# safe at this version.
SCHEMA_VERSION = 1

__all__ = [
    "SCHEMA_VERSION",
    "events_to_dict",
]


def events_to_dict(events: list[dict[str, Any]], *, query: EventQuery) -> dict[str, Any]:
    """Project one event listing onto the versioned wire payload."""
    return {
        "schema_version": SCHEMA_VERSION,
        "chart": query.chart_name,
        "correlation_id": query.correlation_id,
        "limit": query.limit,
        "count": len(events),
        "events": [dict(event) for event in events],
    }
