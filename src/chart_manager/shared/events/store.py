"""EventStore protocol and backend selection (`Settings.events_backend`: cosmos | dynamodb | none).

Events are opt-in: unset means `none`, and nothing is written until
EVENTS_BACKEND is exported.

Both backends partition on `chart_name`, so one chart's whole history is a
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
    EventReadUnsupportedError,
    EventsDisabledError,
    newest_first,
)

# The cloud SDKs load only inside the backend that uses them, so a run
# without events (and `version`) does not pay for azure or boto3.
if TYPE_CHECKING:
    from chart_manager.integrations.cosmos import CosmosContainer
    from chart_manager.integrations.dynamodb import DynamoDBTable
    from chart_manager.settings import EventsBackend, Settings

# The attribute both backends partition on, and DynamoDB's range key. Named
# once so the stores and scripts/provision-event-store cannot drift apart.
PARTITION_KEY = "chart_name"
SORT_KEY = "event_id"

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


class DynamoDBEventStore:
    """Write lifecycle events to DynamoDB (chart_name HASH + synthesized sort key)."""

    def __init__(self, table: DynamoDBTable) -> None:
        """Bind the DynamoDB document table."""
        self._table = table

    def write(self, event: PlatformLifecycleEvent) -> None:
        """Persist one event; requires chart_name (the partition key)."""
        if not event.chart_name:
            raise ValueError("chart_name is required (it is the partition key)")
        item = event.to_dict()

        # Authoritative retry-safe transitions use a stable range key and
        # overwrite their prior attempt. Events without a key remain an
        # append-only, time-ordered stream.
        item[SORT_KEY] = (
            f"idempotent#{event.idempotency_key}"
            if event.idempotency_key is not None
            else f"{item['timestamp']}#{item['uuid']}"
        )

        # boto3's resource serializer rejects tuples; images is a tuple
        item["images"] = list(item["images"])

        # put overwrites retry-safe transitions and appends UUID-backed
        # events. The timestamp remains in the item for chronological reads.
        self._table.put(item)

    def query(self, query: EventQuery) -> list[dict[str, Any]]:
        """Refuse: the read side is Cosmos-only; the write path is unaffected."""
        raise EventReadUnsupportedError(
            "the events read side is Cosmos-only for now; set EVENTS_BACKEND=cosmos to read events"
        )


def _cosmos_store() -> CosmosEventStore:
    from chart_manager.integrations import cosmos

    return CosmosEventStore(cosmos.get_container(COSMOS_DATABASE, EVENTS_RESOURCE))


def _cosmos_preflight() -> Check:
    from chart_manager.integrations import cosmos

    return cosmos.preflight(COSMOS_DATABASE, EVENTS_RESOURCE)


def _dynamodb_store() -> DynamoDBEventStore:
    from chart_manager.integrations import dynamodb

    return DynamoDBEventStore(dynamodb.get_table(EVENTS_RESOURCE))


def _dynamodb_preflight() -> Check:
    from chart_manager.integrations import dynamodb

    return dynamodb.preflight(EVENTS_RESOURCE)


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
    "dynamodb": _Backend(store=_dynamodb_store, preflight=_dynamodb_preflight),
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
