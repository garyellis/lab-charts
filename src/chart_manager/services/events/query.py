"""The read side's selection model and its typed failures.

`EventQuery` is the one shape a store's `query()` accepts, mirroring how
`PlatformLifecycleEvent` is the one shape `write()` accepts. Exactly three
selections exist, because they are the three questions operators ask:

  * nothing set        -- most recent activity across every chart;
  * `chart_name`       -- one chart's whole history (a single-partition read,
                          `chart_name` is the partition key -- see store.py);
  * + `correlation_id` -- one release's timeline within that partition.

The failures are types rather than message substrings so a non-CLI surface
can branch on them, and each carries the `Outcome` it is worth so the surface
exits through `plumbing/exit_codes.py` without owning a second taxonomy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.services.events.ref import ChartSelector

__all__ = [
    "DEFAULT_LIMIT",
    "EventQuery",
    "EventReadError",
    "EventReadUnsupportedError",
    "EventsDisabledError",
    "dynamodb_read_unsupported",
    "newest_first",
]

#: How many events an unqualified `event list` shows. Enough to see a whole
#: promotion chain (a full environment walk is ~9 events) with context.
DEFAULT_LIMIT = 20


class EventReadError(ChartManagerError):
    """A read-side request the configured backend cannot serve.

    Carries the semantic `Outcome` so a surface maps it through
    `exit_code_for` -- both concrete cases are `ENVIRONMENT`: nothing the
    caller asked about failed, the environment simply has no readable ledger.
    """

    def __init__(self, message: str, *, outcome: Outcome = Outcome.ENVIRONMENT) -> None:
        """Attach the exit-code outcome this failure is worth."""
        super().__init__(message)
        self.outcome = outcome


class EventsDisabledError(EventReadError):
    """EVENTS_BACKEND is unset or `none`: there is no ledger to read."""


class EventReadUnsupportedError(EventReadError):
    """The configured backend has no read side (DynamoDB, for now)."""


def dynamodb_read_unsupported() -> EventReadUnsupportedError:
    """The one wording for "reads are Cosmos-only".

    Raised from two places -- the DynamoDB adapter's `query` and the
    dispatch in `store.query_events` that refuses before building the
    adapter -- which must not drift into two different instructions.
    """
    return EventReadUnsupportedError(
        "the events read side is Cosmos-only for now (EVENTS_BACKEND=dynamodb); "
        "read the DynamoDB ledger with scripts/query-events-dynamodb"
    )


@dataclass(frozen=True, slots=True)
class EventQuery:
    """One read-side selection, newest-first, capped at `limit` events."""

    chart_name: str | None = None
    correlation_id: str | None = None
    limit: int = DEFAULT_LIMIT

    def __post_init__(self) -> None:
        """Reject a non-positive limit at the type, whoever built the query."""
        if self.limit < 1:
            raise ValueError(f"limit must be at least 1, got {self.limit}")

    @classmethod
    def from_selector(
        cls, selector: ChartSelector | None, *, limit: int = DEFAULT_LIMIT
    ) -> EventQuery:
        """The three selections, from the optional `CHART[@VERSION]` argument.

        Absent selector: all charts. Bare chart: that chart's history.
        Versioned: one release timeline -- the chart is still set, so the
        backend keeps the single-partition read and narrows within it.
        """
        if selector is None:
            return cls(limit=limit)
        return cls(
            chart_name=selector.name,
            correlation_id=selector.correlation_id,
            limit=limit,
        )


def newest_first(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort stored event documents by their real instant, newest first.

    The ledger keeps timestamps as ISO-8601 *strings*, and documents written
    before the emit boundary normalized to UTC may carry non-UTC offsets --
    which do not compare chronologically as text. The backend's string
    ORDER BY is kept as a cheap pre-sort/cutoff; this re-sort on the parsed
    datetime is what makes the order the caller sees correct.
    """

    def instant(event: dict[str, Any]) -> datetime:
        raw = event.get("timestamp")
        try:
            parsed = datetime.fromisoformat(raw) if isinstance(raw, str) else None
        except ValueError:
            parsed = None
        if parsed is None:
            # A document with no readable stamp sorts oldest rather than
            # crashing the listing it appears in.
            return datetime.min.replace(tzinfo=UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    return sorted(events, key=instant, reverse=True)
