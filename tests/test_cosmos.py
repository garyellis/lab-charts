"""The Cosmos container wrapper, against a fake SDK container proxy."""

from __future__ import annotations

from typing import Any

import pytest

from chart_manager.integrations.cosmos import CosmosContainer


class _FakeProxy:
    """Records what a ContainerProxy was asked to do; replays scripted query items."""

    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items or []
        self.calls: list[tuple[str, Any]] = []

    def create_item(self, item: dict[str, Any]) -> None:
        self.calls.append(("create", item))

    def upsert_item(self, item: dict[str, Any]) -> None:
        self.calls.append(("upsert", item))

    def query_items(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("query", kwargs))
        return list(self.items)


@pytest.mark.parametrize(("upsert", "method"), [(False, "create"), (True, "upsert")])
def test_cosmos_write_creates_or_upserts(upsert: bool, method: str) -> None:
    proxy = _FakeProxy()

    CosmosContainer(proxy).write({"id": "a"}, upsert=upsert)  # type: ignore[arg-type]

    assert proxy.calls == [(method, {"id": "a"})]


@pytest.mark.parametrize(
    ("partition_key", "addressing"),
    [("grafana", {"partition_key": "grafana"}), (None, {"enable_cross_partition_query": True})],
    ids=["one-partition", "cross-partition"],
)
def test_cosmos_query_addresses_the_partition_and_strips_metadata(
    partition_key: str | None, addressing: dict[str, Any]
) -> None:
    proxy = _FakeProxy(items=[{"id": "a", "_rid": "x", "_etag": "y", "_ts": 1}])
    parameters = [{"name": "@limit", "value": 3}]

    items = CosmosContainer(proxy).query(  # type: ignore[arg-type]
        "SELECT * FROM c", parameters, partition_key=partition_key
    )

    assert items == [{"id": "a"}]
    assert proxy.calls == [
        ("query", {"query": "SELECT * FROM c", "parameters": parameters, **addressing})
    ]
