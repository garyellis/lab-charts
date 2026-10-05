"""The DynamoDB table wrapper, against a fake boto3 table."""

from __future__ import annotations

from typing import Any

from chart_manager.integrations.dynamodb import DynamoDBTable


def test_dynamodb_put_passes_the_item() -> None:
    puts: list[dict[str, Any]] = []

    class _FakeTable:
        def put_item(self, *, Item: dict[str, Any]) -> None:  # boto3's own kwarg casing
            puts.append(Item)

    DynamoDBTable(_FakeTable()).put({"chart_name": "loki"})  # type: ignore[arg-type]

    assert puts == [{"chart_name": "loki"}]
