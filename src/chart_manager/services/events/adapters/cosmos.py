"""Cosmos DB-backed EventStore adapter."""
from azure.cosmos import ContainerProxy

from chart_manager.services.events.lifecycle import PlatformLifecycleEvent


class CosmosEventStore:
    """Write lifecycle events to a Cosmos container (chart_name partition key)."""

    def __init__(self, container: ContainerProxy) -> None:
        """Bind the Cosmos container handle."""
        self._container = container

    def write(self, event: PlatformLifecycleEvent) -> None:
        """Persist one event; requires chart_name (the partition key)."""
        if not event.chart_name:
            raise ValueError("chart_name is required (it is the partition key)")
        item = event.to_dict()
        # A stable id turns retrying an authoritative transition into an
        # upsert. Events without one retain the append-only UUID behavior.
        item["id"] = event.idempotency_key or item["uuid"]
        if event.idempotency_key is not None:
            self._container.upsert_item(item)
        else:
            self._container.create_item(item)
