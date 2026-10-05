"""The `event list` wire envelope."""

from __future__ import annotations

from chart_manager.services.events.wire import events_to_dict
from chart_manager.shared.events.query import EventQuery


def test_the_listing_wire_document_carries_its_selection_and_version() -> None:
    query = EventQuery(chart_name="grafana", correlation_id="grafana@1.2.3", limit=5)
    events = [{"chart_name": "grafana", "timestamp": "2026-08-01T12:00:00+00:00"}]

    payload = events_to_dict(events, query=query)

    assert payload == {
        "chart": "grafana",
        "correlation_id": "grafana@1.2.3",
        "limit": 5,
        "count": 1,
        "events": events,
    }
