"""`chart-manager event emit` subcommands.

Thin CLI surface over EventWriter so CI (GitHub Actions) can emit lifecycle
events as shell steps. Emission is non-fatal by default: a failed write logs
a warning and exits 0 so telemetry never breaks a build. --strict-events overrides.

The chart and version arrive as one `CHART@VERSION` positional, parsed by
`services/events/ref.py`. Nothing here looks for an `@`: the token is the
event `correlation_id`, so its grammar is the events domain's, not the
surface's (design commitment 6). The old `--chart` / `--version` flag pair
stays accepted as a hidden alias and reaches the same resolver.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

import typer

from chart_manager.cli._container import container
from chart_manager.services.events.failure import emit_non_fatal
from chart_manager.services.events.lifecycle import BuildPhase, PromotionPhase
from chart_manager.services.events.ref import (
    ChartRef,
    ChartRefError,
    parse_ref,
    ref_from_parts,
)
from chart_manager.services.events.writer import EventWriter

#: The `CHART@VERSION` positional. Optional in the signature only so the
#: deprecated flag pair below can stand in for it; exactly one of the two
#: spellings is required, which `_resolve_ref` enforces.
RefArgument = Annotated[
    str | None,
    typer.Argument(
        metavar="CHART@VERSION",
        help=(
            "The chart release this event is about, e.g. grafana@1.2.3. "
            "Required; it renders as optional only because the deprecated "
            "flag pair may stand in for it."
        ),
    ),
]

#: Deprecated spelling of the positional, kept working per design doc 5
#: ("flags accepted as alias"). Hidden for the same reason a deprecated
#: command name is hidden: `--help` is the documented surface, and a
#: deprecated spelling that advertises itself recruits new callers.
#:
#: `--chart-version` is the primary name -- it matches the schema field
#: (`lifecycle.py`) and does not collide with the CLI's own version (design
#: doc 8.6) -- while `--version` stays accepted because that is the flag
#: actually being aliased.
ChartOption = Annotated[
    str | None,
    typer.Option("--chart", hidden=True, help="Deprecated; use the CHART@VERSION argument."),
]
ChartVersionOption = Annotated[
    str | None,
    typer.Option(
        "--chart-version",
        "--version",
        hidden=True,
        help="Deprecated; use the CHART@VERSION argument.",
    ),
]


def _make_event_writer() -> EventWriter:
    """Build the lifecycle-event writer (module-level so tests can override).

    Comes from the composition root rather than `EventWriter()` inline: the
    container memoizes the writer, so the EventStore it lazily resolves (and
    the Cosmos/DynamoDB client behind it) is built once per container instead
    of once per emitted event. Harmless in a process-per-invocation CLI,
    load-bearing for a long-lived server fronting the same capability.
    """
    return container().event_writer()


def _parse_at(at: str | None) -> datetime | None:
    """Parse an --at ISO-8601 string into a tz-aware datetime (default UTC)."""
    if at is None:
        return None
    try:
        ts = datetime.fromisoformat(at)
    except ValueError as exc:
        raise typer.BadParameter(f"invalid --at timestamp {at!r}: {exc}") from exc
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _resolve_ref(ref: str | None, chart: str | None, chart_version: str | None) -> ChartRef:
    """Turn whichever spelling the caller used into one `ChartRef`.

    The only judgement the surface makes here is *how the caller was
    invoked* -- positional token or deprecated flag pair -- which is the same
    line `cli/plan.py` draws for `--all` versus `--chart` on the CI matrix
    command. Both branches hand raw strings to `services/events/ref.py`,
    which owns what a chart name and a version may be.

    The flag form deliberately does not narrate a deprecation line, and there
    is no flag-level deprecation mechanism anywhere in `cli/`. Every other
    renamed flag on this surface was renamed outright, with its in-repo
    callers updated in the same commit, because every caller of this CLI
    lives in this repository. `--chart`/`--chart-version` survive here only
    because they are a *shape* change (two flags collapsing into one
    positional), not a rename, so there is nothing to alias them to.
    """
    if ref is not None and (chart is not None or chart_version is not None):
        raise typer.BadParameter(
            "give the CHART@VERSION argument or the deprecated "
            "--chart/--chart-version pair, not both"
        )
    try:
        if ref is not None:
            return parse_ref(ref)
        if chart is None:
            raise typer.BadParameter(
                "missing the CHART@VERSION argument, e.g. 'grafana@1.2.3'"
                + (" (--chart-version alone does not name a chart)" if chart_version else "")
            )
        if chart_version is None:
            raise typer.BadParameter(
                "--chart needs a version; prefer the CHART@VERSION argument, "
                "e.g. 'grafana@1.2.3'"
            )
        return ref_from_parts(chart, chart_version)
    except ChartRefError as exc:
        # Narrowed to a usage error, as `_parse_at` already does for `--at`:
        # a malformed argument is exit 2 with usage, not a domain failure.
        raise typer.BadParameter(str(exc)) from exc


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
        # Narration: `event emit build|promote` has no `--output` projection,
        # so this confirmation is not data and must not land on stdout.
        typer.echo(f"emitted {summary}", err=True)


def build(
    phase: Annotated[BuildPhase, typer.Option(help="Build lifecycle phase.")],
    ref: RefArgument = None,
    chart: ChartOption = None,
    chart_version: ChartVersionOption = None,
    build_correlation_id: Annotated[str | None, typer.Option(help="Charts-repo PR.")] = None,
    pr_url: Annotated[str | None, typer.Option(help="PR URL")] = None,
    git_sha: Annotated[str | None, typer.Option(help="Charts-repo commit SHA.")] = None,
    at: Annotated[str | None, typer.Option(help="ISO-8601 event timestamp (default: now). For backfill/seeding.")] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict-events", help="Fail the step on emit error."),
    ] = False,
    ) -> None:
    """Emit a build-lifecycle event (charts repo CI)."""
    # `phase` leads the signature only because Python forbids a required
    # parameter after an optional one, and the positional ref has to be
    # optional for the deprecated flag pair to substitute for it. Click does
    # not care about declaration order for options.
    resolved = _resolve_ref(ref, chart, chart_version)
    timestamp = _parse_at(at)
    _emit(
        _make_event_writer(),
        strict,
        f"build:{phase.value} for {resolved}",
        lambda w: w.build(
            chart_name=resolved.name, chart_version=resolved.version, phase=phase,
            build_correlation_id=build_correlation_id, pr_url=pr_url, git_sha=git_sha,
            timestamp=timestamp,
        ),
    )

def promote(
    environment: Annotated[str, typer.Option("--env", help="Target environment.")],
    phase: Annotated[PromotionPhase, typer.Option(help="Promotion lifecycle phase.")],
    ref: RefArgument = None,
    chart: ChartOption = None,
    chart_version: ChartVersionOption = None,
    promotion_correlation_id: Annotated[str | None, typer.Option(help="Flux-repo PR.")] = None,
    build_correlation_id: Annotated[str | None, typer.Option(help="Originating charts-repo PR.")] = None,
    pr_url: Annotated[str | None, typer.Option(help="PR URL")] = None,
    git_sha: Annotated[str | None, typer.Option(help="Charts-repo commit SHA.")] = None,
    at: Annotated[str | None, typer.Option(help="ISO-8601 event timestamp (default: now). For backfill/seeding.")] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict-events", help="Fail the step on emit error."),
    ] = False,
    ) -> None:
    """Emit a promotion-lifecycle event (flux repo CI)."""
    resolved = _resolve_ref(ref, chart, chart_version)
    timestamp = _parse_at(at)
    _emit(
        _make_event_writer(),
        strict,
        f"promote:{phase.value} for {resolved} -> {environment}",
        lambda w: w.promote(
            chart_name=resolved.name, chart_version=resolved.version,
            environment=environment, phase=phase,
            promotion_correlation_id=promotion_correlation_id,
            build_correlation_id=build_correlation_id, pr_url=pr_url, git_sha=git_sha,
            timestamp=timestamp,
        ),
    )


# --- the command tree ------------------------------------------------------
#
# Assembled here rather than in `cli/main.py` so the whole `event` group --
# including, later, P1b's read side -- is one file. `main.py` mounts it, the
# way it already mounts `upgrade` and `publish`.

emit_app = typer.Typer(no_args_is_help=True, help="Emit one platform lifecycle event.")
emit_app.command("build")(build)
emit_app.command("promote")(promote)

event_app = typer.Typer(no_args_is_help=True, help="Platform lifecycle events.")
event_app.add_typer(emit_app, name="emit")


def register(app: typer.Typer) -> None:
    """Mount the `event` group."""
    app.add_typer(event_app, name="event")


__all__ = ["build", "emit_app", "event_app", "promote", "register"]
