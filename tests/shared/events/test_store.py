"""The event stores' event -> document mapping, and backend selection.

The partition key moved from `correlation_id` (`chart@version`) to
`chart_name`. `correlation_id` remains the *join* key -- DESIGN.md's duration
is grouped by `(correlation_id, environment)` -- but it made a poor partition
key: a fresh partition per version turned "what happened to this chart?" into
a cross-partition fan-out and scattered a chart's history across as many
partitions as it had releases.

These tests pin the key in both stores, because nothing else does: a drift
between the container's declared partition key and the attribute the writer
populates does not fail at write time, it fails as a mis-partitioned document
that queries silently miss. The stores are driven
against fake document containers and tables, the shape `integrations/` hands
them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from chart_manager.plumbing.preflight import CheckStatus
from chart_manager.settings import Settings
from chart_manager.shared.events.model import BuildPhase, PlatformLifecycleEvent
from chart_manager.shared.events.query import EventQuery
from chart_manager.shared.events.store import (
    PARTITION_KEY,
    CosmosEventStore,
    DynamoDBEventStore,
    NullEventStore,
    get_event_store,
    preflight_event_store,
)
from tests.conftest import FakeCosmosContainer


def _event(*, chart_name: str = "loki", version: str | None = "1.2.4") -> PlatformLifecycleEvent:
    return PlatformLifecycleEvent(
        correlation_id=f"{chart_name}@{version}",
        build_correlation_id="owner/repository#7",
        promotion_correlation_id=None,
        chart_name=chart_name,
        chart_version=version,
        images=("ghcr.io/example/loki:1.2.4",),
        environment=None,
        build_phase=BuildPhase.PR_OPEN,
        promotion_phase=None,
        timestamp=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
        source="chart-manager",
        pr_url="https://example.test/pull/7",
        git_sha=None,
        detail={"outcome": "pr_open"},
    )


# ----- doubles -------------------------------------------------------------


class _FakeTable:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def put(self, item: dict[str, Any]) -> None:
        self.items.append(item)


# ----- the partition key ---------------------------------------------------


def test_partition_key_is_the_chart_name() -> None:
    assert PARTITION_KEY == "chart_name"


def test_cosmos_store_writes_the_partition_attribute_and_a_string_id() -> None:
    container = FakeCosmosContainer()
    CosmosEventStore(container).write(_event())

    item = container.items[0]
    assert item[PARTITION_KEY] == "loki"
    # Cosmos requires a string 'id'; the event's uuid supplies uniqueness.
    assert item["id"] == item["uuid"]
    # The join key survives inside the partition.
    assert item["correlation_id"] == "loki@1.2.4"


def test_dynamodb_store_writes_the_partition_attribute_and_a_sortable_key() -> None:
    table = _FakeTable()
    DynamoDBEventStore(table).write(_event())

    item = table.items[0]
    assert item[PARTITION_KEY] == "loki"
    assert item["event_id"] == f"{item['timestamp']}#{item['uuid']}"
    # boto3's resource serializer rejects tuples.
    assert isinstance(item["images"], list)


def test_both_stores_use_stable_keys_for_idempotent_events() -> None:
    event = replace(_event(), idempotency_key="stable-publish-key")
    container = FakeCosmosContainer()
    table = _FakeTable()

    CosmosEventStore(container).write(event)
    DynamoDBEventStore(table).write(event)

    assert container.items == []
    assert container.upserted[0]["id"] == "stable-publish-key"
    assert table.items[0]["event_id"] == "idempotent#stable-publish-key"


@pytest.mark.parametrize(
    "store",
    [
        lambda: CosmosEventStore(FakeCosmosContainer()),
        lambda: DynamoDBEventStore(_FakeTable()),
    ],
    ids=["cosmos", "dynamodb"],
)
def test_both_stores_reject_an_event_without_a_partition_key(store: Any) -> None:
    with pytest.raises(ValueError, match="chart_name"):
        store().write(_event(chart_name=""))


def test_a_versionless_event_is_still_writable() -> None:
    """chart_version is None while a PR is open and nothing is published yet.

    Under the old `correlation_id` partition key this was the awkward case;
    partitioning on the chart makes it unremarkable.
    """
    container = FakeCosmosContainer()
    CosmosEventStore(container).write(_event(version=None))

    assert container.items[0][PARTITION_KEY] == "loki"


# ----- the Cosmos query -----------------------------------------------------


def test_the_all_charts_view_is_a_cross_partition_order_by() -> None:
    container = FakeCosmosContainer()

    CosmosEventStore(container).query(EventQuery(limit=7))

    (sql, parameters, partition_key) = container.queries[0]
    assert sql == "SELECT * FROM c ORDER BY c.timestamp DESC OFFSET 0 LIMIT @limit"
    assert partition_key is None
    assert {"name": "@limit", "value": 7} in parameters


def test_a_chart_query_is_a_single_partition_read() -> None:
    """`chart_name` is the partition key; the query must address it as one
    partition, not fan out and filter."""
    container = FakeCosmosContainer()

    CosmosEventStore(container).query(EventQuery(chart_name="grafana"))

    (sql, parameters, partition_key) = container.queries[0]
    assert "WHERE c.chart_name = @chart_name" in sql
    assert partition_key == "grafana"
    assert {"name": "@chart_name", "value": "grafana"} in parameters


def test_a_release_query_narrows_by_correlation_id_within_the_partition() -> None:
    container = FakeCosmosContainer()

    CosmosEventStore(container).query(
        EventQuery(chart_name="grafana", correlation_id="grafana@1.2.3")
    )

    (sql, parameters, partition_key) = container.queries[0]
    assert "c.chart_name = @chart_name AND c.correlation_id = @correlation_id" in sql
    assert partition_key == "grafana"
    assert {"name": "@correlation_id", "value": "grafana@1.2.3"} in parameters


# ----- backend selection ---------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "store_type"),
    [
        ({}, NullEventStore),
        ({"events_backend": "none"}, NullEventStore),
        ({"events_backend": "cosmos"}, CosmosEventStore),
        ({"events_backend": "dynamodb"}, DynamoDBEventStore),
    ],
    ids=["unset", "none", "cosmos", "dynamodb"],
)
def test_the_backend_setting_selects_the_store(
    configured: dict[str, str], store_type: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Events are opt-in: unset means `none`, a silent sink."""
    monkeypatch.setattr(
        "chart_manager.integrations.cosmos.get_container",
        lambda database, container: FakeCosmosContainer(),
    )
    monkeypatch.setattr("chart_manager.integrations.dynamodb.get_table", lambda name: _FakeTable())

    assert isinstance(get_event_store(Settings(**configured)), store_type)


@pytest.mark.parametrize(
    ("backend", "status", "named"),
    [
        ("none", CheckStatus.SKIPPED, "EVENTS_BACKEND=cosmos"),
        ("cosmos", CheckStatus.FAILED, "COSMOS_ENDPOINT"),
        ("dynamodb", CheckStatus.FAILED, "lifecycle-events"),
    ],
)
def test_preflight_probes_the_configured_backend(
    backend: str, status: CheckStatus, named: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`none` is a supported skip that says how to enable; an unconfigured
    backend fails before any network call."""
    for variable in ("COSMOS_CONNECTION_STRING", "COSMOS_ENDPOINT"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("DYNAMODB_ENDPOINT", "not-a-url")

    (check,) = preflight_event_store(Settings(events_backend=backend))

    assert check.status is status
    assert named in check.detail
