"""EventStore protocol and backend selection (EVENTS_BACKEND: cosmos | dynamodb | none).

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
from typing import Protocol

from chart_manager.integrations.aws.dynamodb.client import get_table
from chart_manager.integrations.azure.cosmos.client import get_container
from chart_manager.services.events.adapters.cosmos import CosmosEventStore
from chart_manager.services.events.adapters.dynamodb import DynamoDBEventStore
from chart_manager.services.events.lifecycle import PlatformLifecycleEvent

# The attribute both backends partition on. Named once so the writer, the
# adapters, and scripts/query-events cannot drift apart.
PARTITION_KEY = "chart_name"


class EventStore(Protocol):
    """Structural interface for an events sink: anything with a `write(event)`."""

    def write(self, event: PlatformLifecycleEvent) -> None:
        """Persist one lifecycle event."""
        ...

class NullEventStore:
    """Drop every event. Selected by EVENTS_BACKEND=none.

    Makes "events are off" a first-class, silent state. Without it the only
    way to run without a backend is to leave Cosmos unconfigured, which
    raises `KeyError: 'COSMOS_ENDPOINT'` on first write -- swallowed as
    non-fatal, but logged as a warning on every single run, which trains
    operators to ignore the one log line that reports genuinely dropped
    telemetry.
    """

    def write(self, event: PlatformLifecycleEvent) -> None:
        """Accept and discard the event."""
        return None

def _build_cosmos_store() -> CosmosEventStore:
    """Wire a CosmosEventStore against the platform/lifecycle-events container."""
    container = get_container(
        database="platform",
        container="lifecycle-events",
        partition_key=f"/{PARTITION_KEY}",
    )
    return CosmosEventStore(container)

def _build_dynamodb_store() -> DynamoDBEventStore:
    """Wire a DynamoDBEventStore against the lifecycle-events table."""
    table = get_table(
        table_name="lifecycle-events",
        partition_key=PARTITION_KEY,
        sort_key="event_id",
    )
    return DynamoDBEventStore(table, sort_key="event_id")

def get_event_store() -> EventStore:
    """Select and build the event store from EVENTS_BACKEND (default cosmos)."""
    backend = os.environ.get("EVENTS_BACKEND", "cosmos")
    if backend == "cosmos":
        return _build_cosmos_store()
    if backend == "dynamodb":
        return _build_dynamodb_store()
    if backend == "none":
        return NullEventStore()
    raise ValueError(f"unsupported EVENTS_BACKEND: {backend!r}")
