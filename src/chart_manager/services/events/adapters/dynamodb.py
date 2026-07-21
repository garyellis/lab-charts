from typing import TYPE_CHECKING

from chart_manager.services.events.lifecycle import PlatformLifecycleEvent

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

class DynamoDBEventStore:
    def __init__(self, table: "Table", *, sort_key: str = "event_id") -> None:
        self._table = table
        self._sort_key = sort_key

    def write(self, event: PlatformLifecycleEvent) -> None:
        if event.correlation_id is None:
            raise ValueError("correlation_id is required (it is the partition key)")
        item = event.to_dict()

        # synthesize the sort key: time-ordered + unique. timestamp is already
        # an ISO-8601 string (lexically sortable), uuid breaks same-instant ties.
        item[self._sort_key] = f"{item['timestamp']}#{item['uuid']}"

        # boto3's resource serializer rejects tuples; images is a tuple
        item["images"] = list(item["images"])

        # put_item overwrites by default, but the unique sort key means each
        # event lands on its own (correlation_id, event_id) pair - no clobber.
        self._table.put_item(Item=item)
