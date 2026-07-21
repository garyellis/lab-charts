"""`chart-manager events` subcommands.
Thin CLI surface over EventWriter so CI (GitHub Actions) can emit lifecycle
events as shell steps. Emission is non-fatal by default: a failed write logs
a warning and exits 0 so telemetry never breaks a build. --strict overrides.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated

import typer

from chart_manager.services.events.lifecycle import BuildPhase, PromotionPhase
from chart_manager.services.events.writer import EventWriter

_LOG = logging.getLogger(__name__)


def _parse_at(at: str | None) -> datetime | None:
    """Parse an --at ISO-8601 string into a tz-aware datetime (default UTC)."""
    if at is None:
        return None
    try:
        ts = datetime.fromisoformat(at)
    except ValueError as exc:
        raise typer.BadParameter(f"invalid --at timestamp {at!r}: {exc}") from exc
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

def _emit(strict: bool, summary: str, fn: Callable[[EventWriter], None]) -> None:
    try:
        fn(EventWriter())
    except Exception as exc: # noqa BLE001 - telemetry must not break the build
        if strict:
            raise
        _LOG.warning(f"event emissions failed (non-fatal): {exc}")
        return
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
    timestamp = _parse_at(at)
    _emit(
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
    app.command("build")(build)
    app.command("promote")(promote)

__all__ = ["build", "promote", "register"]

