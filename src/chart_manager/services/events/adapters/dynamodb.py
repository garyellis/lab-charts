"""DynamoDB-backed EventStore adapter."""
from typing import TYPE_CHECKING

from chart_manager.services.events.lifecycle import PlatformLifecycleEvent

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

        # synthesize the sort key: time-ordered + unique. timestamp is already
        # an ISO-8601 string (lexically sortable), uuid breaks same-instant ties.
        item[self._sort_key] = f"{item['timestamp']}#{item['uuid']}"

        # boto3's resource serializer rejects tuples; images is a tuple
        item["images"] = list(item["images"])

        # put_item overwrites by default, but the unique sort key means each
        # event lands on its own (chart_name, event_id) pair - no clobber.
        # Sorting by timestamp within a chart's partition also makes a chart's
        # history a single ordered Query, no client-side sort.
        self._table.put_item(Item=item)
