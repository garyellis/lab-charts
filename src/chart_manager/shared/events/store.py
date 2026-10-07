"""EventStore protocol and backend selection (`Settings.events_backend`: cosmos | dynamodb | none).

Events are opt-in
-----------------
Unset means `none`: no event is written anywhere until an operator exports
EVENTS_BACKEND=cosmos (or dynamodb). A default that pointed at a
real backend meant every unconfigured run paid for a doomed connection attempt and logged
a swallowed failure -- noise that trains operators to ignore the one warning
that reports genuinely dropped telemetry.

Partitioning
------------
Both backends partition on `chart_name`, not on `correlation_id`.

`correlation_id` (`chart@version`) stays the *join* key -- it is what makes a
version's timeline a timeline, and DESIGN.md's duration is grouped by
`(correlation_id, environment)`. But it is a poor partition key: it mints a
fresh partition per version, so the most common question ("what has happened
to this chart?") becomes a cross-partition fan-out, and a chart's history is
scattered across as many partitions as it has releases.

`chart_name` gives a chart-scoped partition instead: one chart's entire
history -- every version, both lifecycles -- is a single-partition read, and
`correlation_id` narrows within it. At the hundreds-of-events-per-chart-per-year
rate this platform actually produces, partition size is a non-issue; locality
of the queries operators actually run is not.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from chart_manager.plumbing.preflight import Check
from chart_manager.shared.events.model import PlatformLifecycleEvent
from chart_manager.shared.events.query import (
    EventQuery,
    EventsDisabledError,
    dynamodb_read_unsupported,
    newest_first,
)

# The cloud SDKs load only inside the backend that uses them, so a run
# without events (and `version`) does not pay for azure or boto3.
if TYPE_CHECKING:
    from chart_manager.integrations.cosmos import CosmosContainer
    from chart_manager.integrations.dynamodb import DynamoDBTable
    from chart_manager.settings import Settings

# The attribute both backends partition on. Named once so the writer, the
# stores, and scripts/query-events cannot drift apart.
PARTITION_KEY = "chart_name"

# Where the events live, named once so `preflight_event_store` probes exactly
# what `get_event_store` would write to.
COSMOS_DATABASE = "platform"
EVENTS_RESOURCE = "lifecycle-events"


class EventStore(Protocol):
    """Structural interface for an events backend: write one event, query many.

    `query` is part of the protocol even though only Cosmos serves it today:
    a store that cannot read raises a typed `EventReadError` rather than
    being a store with a hole in it, so every backend answers `event list`
    -- some of them with the reason they cannot.
    """

    def write(self, event: PlatformLifecycleEvent) -> None:
        """Persist one lifecycle event."""
        ...

    def query(self, query: EventQuery) -> list[dict[str, Any]]:
        """Return stored event documents matching `query`, newest first."""
        ...

class NullEventStore:
    """Drop every event. Selected when the events backend is unset (the default) or `none`.

    Makes "events are off" a first-class, silent state -- and the default
    one. Without it the only way to run without a backend is to leave Cosmos
    unconfigured, which raises `KeyError: 'COSMOS_ENDPOINT'` on first write
    -- swallowed as non-fatal, but logged as a warning on every single run,
    which trains operators to ignore the one log line that reports genuinely
    dropped telemetry.
    """

    def write(self, event: PlatformLifecycleEvent) -> None:
        """Accept and discard the event."""
        return None

    def query(self, query: EventQuery) -> list[dict[str, Any]]:
        """There is no ledger to read; say so, and say how to get one.

        Writes are silently dropped because telemetry must never break the
        run that produced it; a *read* is the deliverable of the command
        that asked, so silence (an empty list) would be a lie.
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

        A chart-scoped query is a single-partition read (`chart_name` is the
        partition key, and `correlation_id` narrows *within* the partition);
        the unfiltered view fans out across partitions, acceptable at this
        ledger's write rate.

        Indexing assumption: a single-field `ORDER BY c.timestamp` needs only
        Cosmos's *default* indexing policy (every path range-indexed), which
        is exactly what `integrations/cosmos.py::get_container` creates -- it
        never customizes the policy. A future composite ORDER BY (say,
        timestamp within chart) would need a composite index declared there.
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

    def __init__(self, table: DynamoDBTable, *, sort_key: str = "event_id") -> None:
        """Bind the DynamoDB document table and the range-key attribute name."""
        self._table = table
        self._sort_key = sort_key

    def write(self, event: PlatformLifecycleEvent) -> None:
        """Persist one event; requires chart_name (the partition key)."""
        if not event.chart_name:
            raise ValueError("chart_name is required (it is the partition key)")
        item = event.to_dict()

        # Authoritative retry-safe transitions use a stable range key and
        # overwrite their prior attempt. Events without a key remain an
        # append-only, time-ordered stream.
        item[self._sort_key] = (
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
        """Refuse with the Cosmos-only message; the write path is unaffected.

        The all-charts view needs either a Scan or a `chart_name`/`timestamp`
        GSI, and the sort key's `idempotent#` prefix breaks time-ordering
        within a partition -- both deliberately deferred with the DynamoDB
        read side. `scripts/query-events-dynamodb` remains the dev tool.
        """
        raise dynamodb_read_unsupported()


def _build_cosmos_store() -> CosmosEventStore:
    """Wire a CosmosEventStore against the platform/lifecycle-events container."""
    from chart_manager.integrations import cosmos

    container = cosmos.get_container(
        database=COSMOS_DATABASE,
        container=EVENTS_RESOURCE,
        partition_key=f"/{PARTITION_KEY}",
    )
    return CosmosEventStore(container)

def _build_dynamodb_store() -> DynamoDBEventStore:
    """Wire a DynamoDBEventStore against the lifecycle-events table."""
    from chart_manager.integrations import dynamodb

    table = dynamodb.get_table(
        table_name=EVENTS_RESOURCE,
        partition_key=PARTITION_KEY,
        sort_key="event_id",
    )
    return DynamoDBEventStore(table, sort_key="event_id")

def get_event_store(settings: Settings) -> EventStore:
    """Select and build the event store from `settings.events_backend` (default none: opt-in)."""
    if settings.events_backend == "cosmos":
        return _build_cosmos_store()
    if settings.events_backend == "dynamodb":
        return _build_dynamodb_store()
    return NullEventStore()


def query_events(settings: Settings, query: EventQuery) -> list[dict[str, Any]]:
    """Run one read-side selection against the configured backend.

    Lives beside `get_event_store` because this module owns the
    events-backend switch. It short-circuits `dynamodb` on the setting
    rather than calling `get_event_store().query(...)` blind for one
    reason: building the DynamoDB store *provisions* its table
    (`get_table` creates it and blocks on `wait_until_exists`), and a read
    that cannot be served must not touch -- let alone create --
    infrastructure. The `none` case does go through the store, so
    `NullEventStore.query` stays an exercised path rather than a stub.

    Results are re-sorted newest-first client-side; see
    `query.newest_first` for why the backend's string ORDER BY is not
    trusted as chronology.
    """
    if settings.events_backend == "dynamodb":
        raise dynamodb_read_unsupported()
    return newest_first(get_event_store(settings).query(query))


def preflight_event_store(settings: Settings) -> tuple[Check, ...]:
    """Report whether the configured events backend is usable.

    Lives beside `get_event_store` rather than in `doctor` because this is
    the module that owns the events-backend switch: a new backend adds a
    branch here and is reported by `doctor` with no edit to the surface, and
    the two branch tables cannot drift.

    The reachability probe itself belongs to each backend's client, which is
    the integration that knows what "reachable" means for it. This function
    only dispatches -- and answers for `none`, where there is nothing to
    reach: a supported configuration, not a failure. An unsupported value
    never gets here; `load_settings` rejects it.
    """
    if settings.events_backend == "cosmos":
        from chart_manager.integrations import cosmos

        return (cosmos.preflight(COSMOS_DATABASE, EVENTS_RESOURCE),)
    if settings.events_backend == "dynamodb":
        from chart_manager.integrations import dynamodb

        return (dynamodb.preflight(EVENTS_RESOURCE),)
    # The detail names the switch, so the report doubles as the instruction.
    return (
        Check.skipped(
            "events-backend", "events disabled (set EVENTS_BACKEND=cosmos to enable)"
        ),
    )
