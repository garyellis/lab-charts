"""Backend selection and the partition key both stores are keyed on.

The partition key moved from `correlation_id` (`chart@version`) to
`chart_name`. `correlation_id` remains the *join* key -- DESIGN.md's duration
is grouped by `(correlation_id, environment)` -- but it made a poor partition
key: a fresh partition per version turned "what happened to this chart?" into
a cross-partition fan-out and scattered a chart's history across as many
partitions as it had releases.

These tests pin the key in both adapters and in the wiring, because nothing
else does: a drift between the container's declared partition key and the
attribute the writer populates does not fail at write time, it fails as a
mis-partitioned document that queries silently miss.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from chart_manager.services.events import store as store_module
from chart_manager.services.events.adapters.cosmos import CosmosEventStore
from chart_manager.services.events.adapters.dynamodb import DynamoDBEventStore
from chart_manager.services.events.lifecycle import BuildPhase, PlatformLifecycleEvent
from chart_manager.services.events.store import (
    PARTITION_KEY,
    NullEventStore,
    get_event_store,
)


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


class _FakeContainer:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.upserted: list[dict[str, Any]] = []

    def create_item(self, item: dict[str, Any]) -> None:
        self.items.append(item)

    def upsert_item(self, item: dict[str, Any]) -> None:
        self.upserted.append(item)


class _FakeTable:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def put_item(self, *, Item: dict[str, Any]) -> None:  # boto3's own kwarg casing
        self.items.append(Item)


# ----- the partition key ---------------------------------------------------


def test_partition_key_is_the_chart_name() -> None:
    assert PARTITION_KEY == "chart_name"


def test_cosmos_store_writes_the_partition_attribute_and_a_string_id() -> None:
    container = _FakeContainer()
    CosmosEventStore(container).write(_event())

    item = container.items[0]
    assert item[PARTITION_KEY] == "loki"
    # Cosmos requires a string 'id'; the event's uuid supplies uniqueness.
    assert item["id"] == item["uuid"]
    # The join key survives inside the partition.
    assert item["correlation_id"] == "loki@1.2.4"


def test_dynamodb_store_writes_the_partition_attribute_and_a_sortable_key() -> None:
    table = _FakeTable()
    DynamoDBEventStore(table, sort_key="event_id").write(_event())

    item = table.items[0]
    assert item[PARTITION_KEY] == "loki"
    assert item["event_id"] == f"{item['timestamp']}#{item['uuid']}"
    # boto3's resource serializer rejects tuples.
    assert isinstance(item["images"], list)


def test_both_stores_use_stable_keys_for_idempotent_events() -> None:
    event = replace(_event(), idempotency_key="stable-publish-key")
    container = _FakeContainer()
    table = _FakeTable()

    CosmosEventStore(container).write(event)
    DynamoDBEventStore(table, sort_key="event_id").write(event)

    assert container.items == []
    assert container.upserted[0]["id"] == "stable-publish-key"
    assert table.items[0]["event_id"] == "idempotent#stable-publish-key"


@pytest.mark.parametrize(
    "adapter",
    [
        lambda: CosmosEventStore(_FakeContainer()),
        lambda: DynamoDBEventStore(_FakeTable(), sort_key="event_id"),
    ],
    ids=["cosmos", "dynamodb"],
)
def test_both_adapters_reject_an_event_without_a_partition_key(adapter: Any) -> None:
    with pytest.raises(ValueError, match="chart_name"):
        adapter().write(_event(chart_name=""))


def test_a_versionless_event_is_still_writable() -> None:
    """chart_version is None while a PR is open and nothing is published yet.

    Under the old `correlation_id` partition key this was the awkward case;
    partitioning on the chart makes it unremarkable.
    """
    container = _FakeContainer()
    CosmosEventStore(container).write(_event(version=None))

    assert container.items[0][PARTITION_KEY] == "loki"


# ----- backend selection ---------------------------------------------------


def test_cosmos_wiring_declares_the_partition_key_as_a_document_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cosmos wants "/chart_name"; DynamoDB wants the bare attribute name."""
    seen: dict[str, Any] = {}

    def fake_get_container(**kwargs: Any) -> _FakeContainer:
        seen.update(kwargs)
        return _FakeContainer()

    monkeypatch.setattr(store_module, "get_container", fake_get_container)
    monkeypatch.setenv("EVENTS_BACKEND", "cosmos")

    assert isinstance(get_event_store(), CosmosEventStore)
    assert seen["partition_key"] == f"/{PARTITION_KEY}"
    assert seen["database"] == "platform"
    assert seen["container"] == "lifecycle-events"


def test_dynamodb_wiring_declares_the_bare_attribute_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_get_table(**kwargs: Any) -> _FakeTable:
        seen.update(kwargs)
        return _FakeTable()

    monkeypatch.setattr(store_module, "get_table", fake_get_table)
    monkeypatch.setenv("EVENTS_BACKEND", "dynamodb")

    assert isinstance(get_event_store(), DynamoDBEventStore)
    assert seen["partition_key"] == PARTITION_KEY
    assert seen["sort_key"] == "event_id"


def test_events_are_opt_in_unset_selects_the_null_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default backend is `none`: no EVENTS_BACKEND, no writes anywhere.

    Flipped from `cosmos` deliberately (2026-08-02): a telemetry default that
    pointed at a real backend made every unconfigured run log a swallowed
    connection failure.
    """
    monkeypatch.delenv("EVENTS_BACKEND", raising=False)

    assert isinstance(get_event_store(), NullEventStore)


def test_backend_none_is_a_silent_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """"Events off" must be a first-class state, not an unconfigured Cosmos.

    Without this, running without a backend means a swallowed
    KeyError: 'COSMOS_ENDPOINT' warning on every single invocation, which
    trains operators to ignore the one log line that reports dropped
    telemetry.
    """
    monkeypatch.setenv("EVENTS_BACKEND", "none")
    store = get_event_store()

    assert isinstance(store, NullEventStore)
    assert store.write(_event()) is None


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENTS_BACKEND", "sqlite")

    with pytest.raises(ValueError, match="unsupported EVENTS_BACKEND"):
        get_event_store()
