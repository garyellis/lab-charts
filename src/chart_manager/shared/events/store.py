"""EventStore protocol and backend selection (`Settings.events_backend`: cosmos | none).

Events are opt-in: unset means `none`, and nothing is written until
EVENTS_BACKEND is exported.

Events are partitioned on `chart_name`, so one chart's whole history is a
single-partition read; `correlation_id` (`chart@version`) is the join key
that narrows within it.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from chart_manager.plumbing.preflight import Check
from chart_manager.shared.events.model import PlatformLifecycleEvent
from chart_manager.shared.events.query import (
    EventQuery,
    EventsDisabledError,
    newest_first,
)

# The cloud SDKs load only inside the backend that uses them, so a run
# without events (and `version`) does not pay for azure.
if TYPE_CHECKING:
    from chart_manager.integrations.cosmos import CosmosContainer
    from chart_manager.settings import EventsBackend, Settings

# The attribute events are partitioned on. Named once so the stores and
# scripts/provision-event-store cannot drift apart.
PARTITION_KEY = "chart_name"

# Where the events live, named once so `preflight_event_store` probes and
# scripts/provision-event-store creates exactly what `get_event_store` writes to.
COSMOS_DATABASE = "platform"
EVENTS_RESOURCE = "lifecycle-events"


class EventStore(Protocol):
    """Structural interface for an events backend: write one event, query many.

    A store that cannot read raises a typed `EventReadError` from `query`.
    """

    def write(self, event: PlatformLifecycleEvent) -> None:
        """Persist one lifecycle event."""
        ...

    def query(self, query: EventQuery) -> list[dict[str, Any]]:
        """Return stored event documents matching `query`, newest first."""
        ...

class NullEventStore:
    """Drop every event. Selected when the events backend is unset (the default) or `none`."""

    def write(self, event: PlatformLifecycleEvent) -> None:
        """Accept and discard the event."""
        return None

    def query(self, query: EventQuery) -> list[dict[str, Any]]:
        """There is no ledger to read; say so, and say how to get one.

        Writes are dropped silently, but an empty read would be a lie.
        """
        raise EventsDisabledError(
            "events are disabled (EVENTS_BACKEND is unset or 'none'); "
            "set EVENTS_BACKEND=cosmos to record and read lifecycle events"
        )

class CosmosEventStore:
    """Read and write lifecycle events in a Cosmos container (chart_name partition key)."""

    def __init__(self, container: CosmosContainer) -> None:
        """Bind the Cosmos document container."""
        self._container = container

    def write(self, event: PlatformLifecycleEvent) -> None:
        """Persist one event; requires chart_name (the partition key)."""
        if not event.chart_name:
            raise ValueError("chart_name is required (it is the partition key)")
        item = event.to_dict()
        # A stable id turns retrying an authoritative transition into an
        # upsert. Events without one retain the append-only UUID behavior.
        item["id"] = event.idempotency_key or item["uuid"]
        self._container.write(item, upsert=event.idempotency_key is not None)

    def query(self, query: EventQuery) -> list[dict[str, Any]]:
        """Read events newest-first, optionally narrowed by chart / release.

        A single-field ORDER BY needs only Cosmos's default indexing policy,
        which is what `scripts/provision-event-store` creates. Sorting on
        more than one field would need a composite index declared there.
        """
        clauses: list[str] = []
        parameters: list[dict[str, Any]] = [{"name": "@limit", "value": query.limit}]
        if query.chart_name is not None:
            clauses.append("c.chart_name = @chart_name")
            parameters.append({"name": "@chart_name", "value": query.chart_name})
        if query.correlation_id is not None:
            clauses.append("c.correlation_id = @correlation_id")
            parameters.append({"name": "@correlation_id", "value": query.correlation_id})
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._container.query(
            f"SELECT * FROM c{where} ORDER BY c.timestamp DESC OFFSET 0 LIMIT @limit",
            parameters,
            partition_key=query.chart_name,
        )


def _cosmos_store() -> CosmosEventStore:
    from chart_manager.integrations import cosmos

    return CosmosEventStore(cosmos.get_container(COSMOS_DATABASE, EVENTS_RESOURCE))


def _cosmos_preflight() -> Check:
    from chart_manager.integrations import cosmos

    return cosmos.preflight(COSMOS_DATABASE, EVENTS_RESOURCE)


@dataclass(frozen=True, slots=True)
class _Backend:
    """How one `events_backend` value builds its store and probes it."""

    store: Callable[[], EventStore]
    preflight: Callable[[], Check]


# chart-manager's events-backend switch; scripts/provision-event-store keeps
# its own to create the resources.
_BACKENDS: dict[EventsBackend, _Backend] = {
    "none": _Backend(
        store=NullEventStore,
        preflight=lambda: Check.skipped(
            "events-backend", "events disabled (set EVENTS_BACKEND=cosmos to enable)"
        ),
    ),
    "cosmos": _Backend(store=_cosmos_store, preflight=_cosmos_preflight),
}


def get_event_store(settings: Settings) -> EventStore:
    """Build the event store `settings.events_backend` selects (default none: opt-in)."""
    return _BACKENDS[settings.events_backend].store()


def query_events(settings: Settings, query: EventQuery) -> list[dict[str, Any]]:
    """Run one read-side selection against the configured backend's store.

    Results are re-sorted newest-first client-side (see `query.newest_first`).
    """
    return newest_first(get_event_store(settings).query(query))


def preflight_event_store(settings: Settings) -> tuple[Check, ...]:
    """Report whether the configured events backend is usable."""
    return (_BACKENDS[settings.events_backend].preflight(),)
