"""What `publish.run()` takes and returns."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class PublishKind(StrEnum):
    """Meaning of a published artifact in the build lifecycle."""

    PREVIEW = "preview"
    RELEASE = "release"


@dataclass(frozen=True)
class PublishRequest:
    """The charts to publish and where.

    `kind` None is inferred: preview with `version_suffix`, release otherwise. `dry_run`
    prepares every chart the same way but pushes nothing and emits no lifecycle event.
    """

    charts: tuple[str, ...]
    repository: str
    version_suffix: str | None = None
    version: str | None = None
    ca_file: Path | None = None
    kind: PublishKind | None = None
    build_correlation_id: str | None = None
    pr_url: str | None = None
    git_sha: str | None = None
    operation_id: str | None = None
    dry_run: bool = False


@dataclass(frozen=True)
class PublishedChart:
    """One chart's row after the push phase.

    On a dry run this is the planned row: `reference` is the push target, `digest` is None
    because only the registry knows it, and `error` is None because nothing was pushed.
    """

    chart: str
    version: str
    reference: str | None = None
    digest: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class PublishTelemetryFailure:
    """A lifecycle event that could not be persisted after a successful push."""

    chart: str
    version: str
    error: str


@dataclass(frozen=True)
class PublishOutcome:
    """One row per chart, the events that failed, and the kind actually used."""

    charts: tuple[PublishedChart, ...]
    kind: PublishKind
    telemetry_failures: tuple[PublishTelemetryFailure, ...] = ()
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(chart.ok for chart in self.charts)

    @property
    def telemetry_ok(self) -> bool:
        return not self.telemetry_failures
