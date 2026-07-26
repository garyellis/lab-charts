"""EventWriter: the capability layer that builds lifecycle events and writes them to a store."""
from datetime import UTC, datetime

from chart_manager.services.events.lifecycle import (
    BuildPhase,
    PlatformLifecycleEvent,
    PromotionPhase,
)
from chart_manager.services.events.store import EventStore, get_event_store


class EventWriter:
    """Assemble PlatformLifecycleEvents and persist them via a lazily-resolved EventStore."""

    def __init__(self, store: EventStore | None = None, *, source = "chart-manager") -> None:
        """Optionally inject a store; otherwise it's resolved on first write."""
        self._store = store    # resolved lazily so constructing is free
        self._source = source

    def _get_store(self) -> EventStore:
        """Return the store, resolving it from EVENTS_BACKEND on first use."""
        if self._store is None:
            self._store =  get_event_store()
        return self._store

    def build(
        self,
        *,
        chart_name: str,
        chart_version: str | None,
        phase: BuildPhase,
        build_correlation_id: str | None = None, # the charts-repo PR, passed in
        images: tuple[str, ...] = (),
        pr_url: str | None = None,
        git_sha: str | None = None,
        detail: dict | None = None,
        timestamp: datetime | None = None,  # override now() for backfill/seeding
    ) -> None:
        """Build and write a build-lifecycle event for a chart."""
        event = PlatformLifecycleEvent(
            correlation_id=f"{chart_name}@{chart_version}",
            build_correlation_id=build_correlation_id,
            promotion_correlation_id=None,
            chart_name=chart_name,
            chart_version=chart_version,
            images=images,
            environment=None,
            build_phase=phase,
            promotion_phase=None,
            timestamp=timestamp or datetime.now(UTC),
            source=self._source,
            pr_url=pr_url,
            git_sha=git_sha,
            detail=detail,
        )
        self._get_store().write(event)

    def promote(
        self,
        *,
        chart_name: str,
        chart_version: str,
        environment: str,
        phase: PromotionPhase,
        images: tuple[str, ...] = (),                 # resolved
        promotion_correlation_id: str | None = None,  # the flux pr
        build_correlation_id: str | None = None,      # optional denorm
        pr_url: str | None = None,
        git_sha: str | None = None,
        detail: dict | None = None,
        timestamp: datetime | None = None,  # override now() for backfill/seeding
    ) -> None:
       """Build and write a promotion-lifecycle event for a chart in an environment."""
       event = PlatformLifecycleEvent(
           correlation_id=f"{chart_name}@{chart_version}",
           build_correlation_id=build_correlation_id,
           promotion_correlation_id=promotion_correlation_id,
           chart_name=chart_name,
           chart_version=chart_version,
           images=images,
           environment=environment,
           build_phase=None,
           promotion_phase=phase,
           timestamp=timestamp or datetime.now(UTC),
           source=self._source,
           pr_url=pr_url,
           git_sha=git_sha,
           detail=detail,
       )
       self._get_store().write(event)
