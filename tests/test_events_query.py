"""The events read side: selection model, Cosmos query, and backend dispatch.

Three layers, tested at their own seams: `EventQuery` (what a selection
means), `CosmosEventStore.query` (what SQL and addressing that selection
becomes, against a recording fake container), and `store.query_events` (which
backends can serve a read at all, and the typed refusals from the ones that
cannot).
"""

from __future__ import annotations

from typing import Any

import pytest

from chart_manager.services.events import store as store_module
from chart_manager.services.events.adapters.cosmos import CosmosEventStore
from chart_manager.services.events.adapters.dynamodb import DynamoDBEventStore
from chart_manager.services.events.query import (
    DEFAULT_LIMIT,
    EventQuery,
    EventReadUnsupportedError,
    EventsDisabledError,
    newest_first,
)
from chart_manager.services.events.ref import parse_selector
from chart_manager.services.events.store import NullEventStore, query_events
from chart_manager.services.events.wire import SCHEMA_VERSION, events_to_dict


def _doc(**overrides: Any) -> dict[str, Any]:
    """One stored ledger document, minimally shaped like `to_dict` output."""
    doc: dict[str, Any] = {
        "chart_name": "grafana",
        "chart_version": "1.2.3",
        "correlation_id": "grafana@1.2.3",
        "build_phase": "published",
        "promotion_phase": None,
        "environment": None,
        "source": "chart-manager",
        "timestamp": "2026-08-01T12:00:00+00:00",
    }
    doc.update(overrides)
    return doc


# ----- the selection model -------------------------------------------------


def test_no_selector_selects_recent_activity_across_all_charts() -> None:
    query = EventQuery.from_selector(None)

    assert query == EventQuery(chart_name=None, correlation_id=None, limit=DEFAULT_LIMIT)


def test_a_bare_chart_selects_that_charts_history() -> None:
    query = EventQuery.from_selector(parse_selector("grafana"), limit=5)

    assert query == EventQuery(chart_name="grafana", correlation_id=None, limit=5)


def test_a_versioned_selector_keeps_the_chart_and_narrows_to_the_release() -> None:
    """The chart stays set so the backend keeps the single-partition read."""
    query = EventQuery.from_selector(parse_selector("grafana@1.2.3"))

    assert query.chart_name == "grafana"
    assert query.correlation_id == "grafana@1.2.3"


def test_a_non_positive_limit_is_rejected_at_the_type() -> None:
    with pytest.raises(ValueError, match="limit"):
        EventQuery(limit=0)


# ----- newest-first, including the mixed-timezone regression ---------------


def test_newest_first_orders_by_instant_not_by_string() -> None:
    """The regression this exists for: `2026-08-01T14:30:00+02:00` is
    *older* than `2026-08-01T13:00:00+00:00` but sorts newer as a string."""
    utc = _doc(timestamp="2026-08-01T13:00:00+00:00")
    offset = _doc(timestamp="2026-08-01T14:30:00+02:00")  # 12:30 UTC

    assert newest_first([offset, utc]) == [utc, offset]


def test_newest_first_reads_a_naive_stamp_as_utc_and_survives_garbage() -> None:
    fresh = _doc(timestamp="2026-08-01T12:00:00")
    older = _doc(timestamp="2026-08-01T11:00:00+00:00")
    broken = _doc(timestamp="not-a-time")

    assert newest_first([broken, older, fresh]) == [fresh, older, broken]


# ----- the Cosmos adapter ---------------------------------------------------


class _RecordingContainer:
    """Fake ContainerProxy: records the query call, replays scripted items."""

    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items or []
        self.calls: list[dict[str, Any]] = []

    def query_items(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return list(self.items)


def _one_call(container: _RecordingContainer) -> dict[str, Any]:
    (call,) = container.calls
    return call


def test_the_all_charts_view_is_a_cross_partition_order_by() -> None:
    container = _RecordingContainer()

    CosmosEventStore(container).query(EventQuery(limit=7))

    call = _one_call(container)
    assert call["query"] == (
        "SELECT * FROM c ORDER BY c.timestamp DESC OFFSET 0 LIMIT @limit"
    )
    assert call["enable_cross_partition_query"] is True
    assert {"name": "@limit", "value": 7} in call["parameters"]


def test_a_chart_query_is_a_single_partition_read() -> None:
    """`chart_name` is the partition key; the query must address it as one
    partition, not fan out and filter."""
    container = _RecordingContainer()

    CosmosEventStore(container).query(EventQuery(chart_name="grafana"))

    call = _one_call(container)
    assert "WHERE c.chart_name = @chart_name" in call["query"]
    assert call["partition_key"] == "grafana"
    assert "enable_cross_partition_query" not in call
    assert {"name": "@chart_name", "value": "grafana"} in call["parameters"]


def test_a_release_query_narrows_by_correlation_id_within_the_partition() -> None:
    container = _RecordingContainer()

    CosmosEventStore(container).query(
        EventQuery(chart_name="grafana", correlation_id="grafana@1.2.3")
    )

    call = _one_call(container)
    assert "c.chart_name = @chart_name AND c.correlation_id = @correlation_id" in call["query"]
    assert call["partition_key"] == "grafana"
    assert {"name": "@correlation_id", "value": "grafana@1.2.3"} in call["parameters"]


def test_cosmos_bookkeeping_properties_are_stripped_from_results() -> None:
    """_rid/_etag/_ts are transport, not ledger; no other backend has them."""
    container = _RecordingContainer(
        items=[_doc(_rid="x", _etag='"y"', _ts=123, id="abc")]
    )

    (item,) = CosmosEventStore(container).query(EventQuery())

    assert "id" in item
    assert not [key for key in item if key.startswith("_")]


# ----- the refusals ---------------------------------------------------------


def test_the_null_store_refuses_a_read_and_says_how_to_enable() -> None:
    with pytest.raises(EventsDisabledError, match="EVENTS_BACKEND"):
        NullEventStore().query(EventQuery())


def test_the_dynamodb_store_refuses_a_read_and_points_at_the_script() -> None:
    with pytest.raises(EventReadUnsupportedError, match="query-events-dynamodb"):
        DynamoDBEventStore(object(), sort_key="event_id").query(EventQuery())  # type: ignore[arg-type]


# ----- backend dispatch -----------------------------------------------------


def test_query_events_with_backend_none_raises_the_disabled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENTS_BACKEND", "none")

    with pytest.raises(EventsDisabledError):
        query_events(EventQuery())


def test_query_events_with_dynamodb_refuses_without_touching_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`get_table` *creates* the table and blocks on wait_until_exists; a
    read that cannot be served must never reach it."""
    monkeypatch.setenv("EVENTS_BACKEND", "dynamodb")
    monkeypatch.setattr(
        store_module,
        "get_table",
        lambda **kwargs: pytest.fail("query_events built the DynamoDB store"),
    )

    with pytest.raises(EventReadUnsupportedError, match="Cosmos-only"):
        query_events(EventQuery())


def test_query_events_with_cosmos_returns_the_page_re_sorted_newest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backend ORDER BY is over strings; the caller sees chronology."""
    utc = _doc(timestamp="2026-08-01T13:00:00+00:00")
    offset = _doc(timestamp="2026-08-01T14:30:00+02:00")  # 12:30 UTC
    container = _RecordingContainer(items=[offset, utc])  # string order
    monkeypatch.setenv("EVENTS_BACKEND", "cosmos")
    monkeypatch.setattr(store_module, "get_container", lambda **kwargs: container)

    assert query_events(EventQuery()) == [utc, offset]


# ----- the wire envelope ----------------------------------------------------


def test_the_listing_wire_document_carries_its_selection_and_version() -> None:
    query = EventQuery(chart_name="grafana", correlation_id="grafana@1.2.3", limit=5)
    events = [_doc()]

    payload = events_to_dict(events, query=query)

    assert payload == {
        "schema_version": SCHEMA_VERSION,
        "chart": "grafana",
        "correlation_id": "grafana@1.2.3",
        "limit": 5,
        "count": 1,
        "events": events,
    }
