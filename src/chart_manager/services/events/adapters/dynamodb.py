"""DynamoDB-backed EventStore adapter (write-only; the read side is Cosmos-only)."""
from typing import TYPE_CHECKING, Any

from chart_manager.services.events.lifecycle import PlatformLifecycleEvent
from chart_manager.services.events.query import EventQuery, dynamodb_read_unsupported

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

class DynamoDBEventStore:
    """Write lifecycle events to DynamoDB (chart_name HASH + synthesized sort key)."""

    def __init__(self, table: "Table", *, sort_key: str = "event_id") -> None:
        """Bind the boto3 Table and the range-key attribute name."""
        self._table = table
        self._sort_key = sort_key

    def write(self, event: PlatformLifecycleEvent) -> None:
        """Persist one event; requires chart_name (the partition key)."""
        if not event.chart_name:
            raise ValueError("chart_name is required (it is the partition key)")
        item = event.to_dict()

        # Authoritative retry-safe transitions use a stable range key and
        # overwrite their prior attempt. Legacy/ad-hoc events remain an
        # append-only, time-ordered stream.
        item[self._sort_key] = (
            f"idempotent#{event.idempotency_key}"
            if event.idempotency_key is not None
            else f"{item['timestamp']}#{item['uuid']}"
        )

        # boto3's resource serializer rejects tuples; images is a tuple
        item["images"] = list(item["images"])

        # put_item overwrites retry-safe transitions and appends UUID-backed
        # events. The timestamp remains in the item for chronological reads.
        self._table.put_item(Item=item)

    def query(self, query: EventQuery) -> list[dict[str, Any]]:  # noqa: ARG002
        """Refuse with the Cosmos-only message; the write path is unaffected.

        The all-charts view needs either a Scan or a `chart_name`/`timestamp`
        GSI, and the sort key's `idempotent#` prefix breaks time-ordering
        within a partition -- both deliberately deferred with the DynamoDB
        read side. `scripts/query-events-dynamodb` remains the dev tool.
        """
        raise dynamodb_read_unsupported()
