"""EventWriter: the capability layer that builds lifecycle events and writes them to a store."""
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from chart_manager.shared.events.model import (
    BuildPhase,
    PlatformLifecycleEvent,
    PromotionPhase,
)
from chart_manager.shared.events.store import EventStore


class EventWriter:
    """Assemble PlatformLifecycleEvents and persist them via a lazily-resolved EventStore."""

    def __init__(self, source: str, store: Callable[[], EventStore]) -> None:
        """Bind the event source and the store factory, called on the first write.

        Resolving lazily means a run that never emits never touches the backend.
        """
        self._source = source
        self._store_factory = store
        self._store: EventStore | None = None

    def _get_store(self) -> EventStore:
        """Return the store, resolving it from the factory on first use."""
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    def compose_build(
        self,
        *,
        chart_name: str,
        chart_version: str | None,
        phase: BuildPhase,
        build_correlation_id: str | None = None, # the charts-repo PR, passed in
        images: tuple[str, ...] = (),
        pr_url: str | None = None,
        git_sha: str | None = None,
        detail: dict[str, Any] | None = None,
        timestamp: datetime | None = None,  # override now() for backfill/seeding
        idempotency_key: str | None = None,
    ) -> PlatformLifecycleEvent:
        """Assemble a build-lifecycle event without writing it.

        Split from `build` so a `--dry-run` can show exactly the document a
        real run would persist, without the store -- and therefore the
        backend -- ever being resolved.
        """
        return PlatformLifecycleEvent(
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
            idempotency_key=idempotency_key,
        )

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
        detail: dict[str, Any] | None = None,
        timestamp: datetime | None = None,  # override now() for backfill/seeding
        idempotency_key: str | None = None,
    ) -> None:
        """Build and write a build-lifecycle event for a chart."""
        event = self.compose_build(
            chart_name=chart_name,
            chart_version=chart_version,
            phase=phase,
            build_correlation_id=build_correlation_id,
            images=images,
            pr_url=pr_url,
            git_sha=git_sha,
            detail=detail,
            timestamp=timestamp,
            idempotency_key=idempotency_key,
        )
        self._get_store().write(event)

    def compose_promote(
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
        detail: dict[str, Any] | None = None,
        timestamp: datetime | None = None,  # override now() for backfill/seeding
    ) -> PlatformLifecycleEvent:
        """Assemble a promotion-lifecycle event without writing it.

        The `--dry-run` seam; see `compose_build`.
        """
        return PlatformLifecycleEvent(
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
            idempotency_key=None,
        )

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
        detail: dict[str, Any] | None = None,
        timestamp: datetime | None = None,  # override now() for backfill/seeding
    ) -> None:
        """Build and write a promotion-lifecycle event for a chart in an environment."""
        event = self.compose_promote(
            chart_name=chart_name,
            chart_version=chart_version,
            environment=environment,
            phase=phase,
            images=images,
            promotion_correlation_id=promotion_correlation_id,
            build_correlation_id=build_correlation_id,
            pr_url=pr_url,
            git_sha=git_sha,
            detail=detail,
            timestamp=timestamp,
        )
        self._get_store().write(event)
