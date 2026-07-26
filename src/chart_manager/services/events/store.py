"""EventStore protocol and backend selection (EVENTS_BACKEND: cosmos | dynamodb)."""
import os
from typing import Protocol

from chart_manager.integrations.aws.dynamodb.client import get_table
from chart_manager.integrations.azure.cosmos.client import get_container
from chart_manager.services.events.adapters.cosmos import CosmosEventStore
from chart_manager.services.events.adapters.dynamodb import DynamoDBEventStore
from chart_manager.services.events.lifecycle import PlatformLifecycleEvent


class EventStore(Protocol):
    """Structural interface for an events sink: anything with a `write(event)`."""

    def write(self, event: PlatformLifecycleEvent) -> None:
        """Persist one lifecycle event."""
        ...

def _build_cosmos_store() -> CosmosEventStore:
    """Wire a CosmosEventStore against the platform/lifecycle-events container."""
    container = get_container(
        database="platform",
        container="lifecycle-events",
        partition_key="/correlation_id",
    )
    return CosmosEventStore(container)

def _build_dynamodb_store() -> DynamoDBEventStore:
    """Wire a DynamoDBEventStore against the lifecycle-events table."""
    table = get_table(
        table_name="lifecycle-events",
        partition_key="correlation_id",
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
    raise ValueError(f"unsupported EVENTS_BACKEND: {backend!r}")
