"""Cosmos DB-backed EventStore adapter."""
from typing import Any

from azure.cosmos import ContainerProxy

from chart_manager.services.events.lifecycle import PlatformLifecycleEvent
from chart_manager.services.events.query import EventQuery


class CosmosEventStore:
    """Read and write lifecycle events in a Cosmos container (chart_name partition key)."""

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
        kwargs: dict[str, Any] = {"enable_cross_partition_query": True}
        if query.chart_name is not None:
            clauses.append("c.chart_name = @chart_name")
            parameters.append({"name": "@chart_name", "value": query.chart_name})
            kwargs = {"partition_key": query.chart_name}
        if query.correlation_id is not None:
            clauses.append("c.correlation_id = @correlation_id")
            parameters.append({"name": "@correlation_id", "value": query.correlation_id})
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        items = self._container.query_items(
            query=(
                f"SELECT * FROM c{where} "
                "ORDER BY c.timestamp DESC OFFSET 0 LIMIT @limit"
            ),
            parameters=parameters,
            **kwargs,
        )
        # `_`-prefixed keys are Cosmos bookkeeping (_rid, _etag, _ts, ...):
        # transport metadata, not ledger content, and no other backend would
        # produce them.
        return [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in items
        ]
