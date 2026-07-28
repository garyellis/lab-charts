"""`chart-manager events` subcommands.
Thin CLI surface over EventWriter so CI (GitHub Actions) can emit lifecycle
events as shell steps. Emission is non-fatal by default: a failed write logs
a warning and exits 0 so telemetry never breaks a build. --strict overrides.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

import typer

from chart_manager.composition import Container
from chart_manager.services.events.failure import emit_non_fatal
from chart_manager.services.events.lifecycle import BuildPhase, PromotionPhase
from chart_manager.services.events.writer import EventWriter


def _make_event_writer() -> EventWriter:
    """Build the lifecycle-event writer (module-level so tests can override).

    Comes from the composition root rather than `EventWriter()` inline: the
    container memoizes the writer, so the EventStore it lazily resolves (and
    the Cosmos/DynamoDB client behind it) is built once per container instead
    of once per emitted event. Harmless in a process-per-invocation CLI,
    load-bearing for a long-lived server fronting the same capability.
    """
    return Container().event_writer()


def _parse_at(at: str | None) -> datetime | None:
    """Parse an --at ISO-8601 string into a tz-aware datetime (default UTC)."""
    if at is None:
        return None
    try:
        ts = datetime.fromisoformat(at)
    except ValueError as exc:
        raise typer.BadParameter(f"invalid --at timestamp {at!r}: {exc}") from exc
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)

def _emit(
    writer: EventWriter, strict: bool, summary: str, fn: Callable[[EventWriter], None]
) -> None:
    """Run an emit callback; swallow+warn on failure unless `strict` (telemetry is non-fatal).

    The writer is built by the caller, but backend resolution still happens
    lazily on first write -- i.e. inside `fn` and therefore inside the shared
    `emit_non_fatal` boundary -- so a misconfigured EVENTS_BACKEND stays
    non-fatal.

    The confirmation line is printed only on success: `emit_non_fatal`
    swallows, so `emitted ...` would otherwise be echoed for events that were
    dropped.
    """
    emitted = False

    def run() -> None:
        nonlocal emitted
        fn(writer)
        emitted = True

    emit_non_fatal(run, strict=strict, what=summary)
    if emitted:
        typer.echo(f"emitted {summary}")

def build(
    chart: Annotated[str, typer.Option(help="Chart name.")],
    version: Annotated[str, typer.Option(help="Chart version.")],
    phase: Annotated[BuildPhase, typer.Option(help="Build lifecycle phase.")],
    build_correlation_id: Annotated[str | None, typer.Option(help="Charts-repo PR.")] = None,
    pr_url: Annotated[str | None, typer.Option(help="PR URL")] = None,
    git_sha: Annotated[str | None, typer.Option(help="Charts-repo commit SHA.")] = None,
    at: Annotated[str | None, typer.Option(help="ISO-8601 event timestamp (default: now). For backfill/seeding.")] = None,
    strict: Annotated[bool, typer.Option(help="Fail the step on emit error.")] = False,
    ) -> None:
    """Emit a build-lifecycle event (charts repo CI)."""
    timestamp = _parse_at(at)
    _emit(
        _make_event_writer(),
        strict,
        f"build:{phase.value} for {chart}@{version}",
        lambda w: w.build(
            chart_name=chart, chart_version=version, phase=phase,
            build_correlation_id=build_correlation_id, pr_url=pr_url, git_sha=git_sha,
            timestamp=timestamp,
        ),
    )

def promote(
    chart: Annotated[str, typer.Option(help="Chart name.")],
    version: Annotated[str, typer.Option(help="Chart version.")],
    environment: Annotated[str, typer.Option(help="Target environment.")],
    phase: Annotated[PromotionPhase, typer.Option(help="Promotion lifecycle phase.")],
    promotion_correlation_id: Annotated[str | None, typer.Option(help="Flux-repo PR.")] = None,
    build_correlation_id: Annotated[str | None, typer.Option(help="Originating charts-repo PR.")] = None,
    pr_url: Annotated[str | None, typer.Option(help="PR URL")] = None,
    git_sha: Annotated[str | None, typer.Option(help="Charts-repo commit SHA.")] = None,
    at: Annotated[str | None, typer.Option(help="ISO-8601 event timestamp (default: now). For backfill/seeding.")] = None,
    strict: Annotated[bool, typer.Option(help="Fail the step on emit error.")] = False,
    ) -> None:
    """Emit a promotion-lifecycle event (flux repo CI)."""
    timestamp = _parse_at(at)
    _emit(
        _make_event_writer(),
        strict,
        f"promote:{phase.value} for {chart}@{version} -> {environment}",
        lambda w: w.promote(
            chart_name=chart, chart_version=version, environment=environment, phase=phase,
            promotion_correlation_id=promotion_correlation_id,
            build_correlation_id=build_correlation_id, pr_url=pr_url, git_sha=git_sha,
            timestamp=timestamp,
        ),
    )

def register(app: typer.Typer) -> None:
    """Attach the build/promote commands to the given Typer app."""
    app.command("build")(build)
    app.command("promote")(promote)

__all__ = ["build", "promote", "register"]

