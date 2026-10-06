"""The events read side: the selection model and backend dispatch.

`EventQuery` (what a selection means) and `store.query_events` (which
backends can serve a read at all, and the typed refusals from the ones that
cannot). The SQL a selection becomes is pinned in `test_store.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from chart_manager.shared.events.query import (
    DEFAULT_LIMIT,
    EventQuery,
    EventReadUnsupportedError,
    EventsDisabledError,
    newest_first,
)
from chart_manager.shared.events.ref import parse_selector
from chart_manager.shared.events.store import NullEventStore, query_events


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


# ----- the refusals ---------------------------------------------------------


def test_the_null_store_refuses_a_read_and_says_how_to_enable() -> None:
    with pytest.raises(EventsDisabledError, match="EVENTS_BACKEND"):
        NullEventStore().query(EventQuery())


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
        "chart_manager.integrations.dynamodb.get_table",
        lambda **kwargs: pytest.fail("query_events built the DynamoDB store"),
    )

    with pytest.raises(EventReadUnsupportedError, match="Cosmos-only"):
        query_events(EventQuery())
