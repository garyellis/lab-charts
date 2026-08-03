"""EventStore protocol and backend selection (EVENTS_BACKEND: cosmos | dynamodb | none).

Events are opt-in
-----------------
Unset means `none`: no event is written anywhere until an operator exports
EVENTS_BACKEND=cosmos (or dynamodb). A default that pointed at a real backend
meant every unconfigured run paid for a doomed connection attempt and logged
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
import os
from typing import Any, Protocol

from chart_manager.integrations import cosmos as cosmos_client
from chart_manager.integrations import dynamodb as dynamodb_client
from chart_manager.integrations.cosmos import get_container
from chart_manager.integrations.dynamodb import get_table
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import Check
from chart_manager.services.events.adapters.cosmos import CosmosEventStore
from chart_manager.services.events.adapters.dynamodb import DynamoDBEventStore
from chart_manager.services.events.lifecycle import PlatformLifecycleEvent
from chart_manager.services.events.query import (
    EventQuery,
    EventsDisabledError,
    dynamodb_read_unsupported,
    newest_first,
)

# The attribute both backends partition on. Named once so the writer, the
# adapters, and scripts/query-events cannot drift apart.
PARTITION_KEY = "chart_name"

# Where the events live, named once so `preflight_event_store` probes exactly
# what `get_event_store` would write to.
COSMOS_DATABASE = "platform"
EVENTS_RESOURCE = "lifecycle-events"

#: The backend when EVENTS_BACKEND is unset. `none`: events are opt-in, and
#: an environment that never asked for telemetry writes nothing anywhere.
DEFAULT_BACKEND = "none"


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
    """Drop every event. Selected when EVENTS_BACKEND is unset (the default) or `none`.

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

def _build_cosmos_store() -> CosmosEventStore:
    """Wire a CosmosEventStore against the platform/lifecycle-events container."""
    container = get_container(
        database=COSMOS_DATABASE,
        container=EVENTS_RESOURCE,
        partition_key=f"/{PARTITION_KEY}",
    )
    return CosmosEventStore(container)

def _build_dynamodb_store() -> DynamoDBEventStore:
    """Wire a DynamoDBEventStore against the lifecycle-events table."""
    table = get_table(
        table_name=EVENTS_RESOURCE,
        partition_key=PARTITION_KEY,
        sort_key="event_id",
    )
    return DynamoDBEventStore(table, sort_key="event_id")

def get_event_store() -> EventStore:
    """Select and build the event store from EVENTS_BACKEND (default none: opt-in)."""
    backend = os.environ.get("EVENTS_BACKEND", DEFAULT_BACKEND)
    if backend == "cosmos":
        return _build_cosmos_store()
    if backend == "dynamodb":
        return _build_dynamodb_store()
    if backend == "none":
        return NullEventStore()
    raise ValueError(f"unsupported EVENTS_BACKEND: {backend!r}")


def query_events(query: EventQuery) -> list[dict[str, Any]]:
    """Run one read-side selection against the configured backend.

    Lives beside `get_event_store` because this module owns the
    EVENTS_BACKEND switch. It short-circuits `dynamodb` on the variable
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
    if os.environ.get("EVENTS_BACKEND", DEFAULT_BACKEND) == "dynamodb":
        raise dynamodb_read_unsupported()
    return newest_first(get_event_store().query(query))


def preflight_event_store() -> tuple[Check, ...]:
    """Report whether the configured events backend is usable.

    Lives beside `get_event_store` rather than in `doctor` because this is
    the module that owns the EVENTS_BACKEND switch: a new backend adds a
    branch here and is reported by `doctor` with no edit to the surface, and
    the two branch tables cannot drift.

    The reachability probe itself belongs to each backend's client, which is
    the integration that knows what "reachable" means for it. This function
    only dispatches -- and answers for the two cases where there is nothing
    to reach: `none`, which is a supported configuration and not a failure,
    and an unrecognised value, which is `Outcome.SPEC` because the operator
    wrote something wrong rather than the environment being down.
    """
    backend = os.environ.get("EVENTS_BACKEND", DEFAULT_BACKEND)
    if backend == "none":
        # Disabled is the default; the unset case names the switch so the
        # report doubles as the instruction for turning events on.
        detail = (
            "EVENTS_BACKEND=none (events disabled)"
            if "EVENTS_BACKEND" in os.environ
            else "events disabled (EVENTS_BACKEND unset; set EVENTS_BACKEND=cosmos to enable)"
        )
        return (Check.skipped("events-backend", detail),)
    if backend == "cosmos":
        return (cosmos_client.preflight(COSMOS_DATABASE, EVENTS_RESOURCE),)
    if backend == "dynamodb":
        return (dynamodb_client.preflight(EVENTS_RESOURCE),)
    return (
        Check.failed(
            "events-backend",
            f"unsupported EVENTS_BACKEND: {backend!r}",
            remediation="set EVENTS_BACKEND to one of: cosmos, dynamodb, none",
            outcome=Outcome.SPEC,
        ),
    )
