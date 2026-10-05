"""`chart-manager event emit|list` subcommands.

Thin CLI surface over EventWriter so CI (GitHub Actions) can emit lifecycle
events as shell steps, plus the read side: `event list` over the same
ledger. Emission is non-fatal by default: a failed write logs a warning and
exits 0 so telemetry never breaks a build. --strict-events overrides. A
*read* is the opposite -- the listing is the deliverable, so an unreadable
backend is a reported failure, exiting through the typed `EventReadError`'s
own outcome.

The chart and version arrive as one `CHART@VERSION` positional (`event
list` takes the optional-version `CHART[@VERSION]` selector), parsed by
`shared/events/ref.py`. Nothing here looks for an `@`: the token is the
event `correlation_id`, so its grammar is the events domain's, not the
surface's (design commitment 6). The old `--chart` / `--version` flag pair
stays accepted as a hidden alias and reaches the same resolver.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from chart_manager.cli import output as output_mod
from chart_manager.cli._container import container
from chart_manager.cli.streams import console, errors, narration
from chart_manager.plumbing.exit_codes import exit_code_for
from chart_manager.services.events.wire import events_to_dict
from chart_manager.shared.events.failure import emit_non_fatal
from chart_manager.shared.events.model import BuildPhase, PromotionPhase
from chart_manager.shared.events.query import (
    DEFAULT_LIMIT,
    EventQuery,
    EventReadError,
)
from chart_manager.shared.events.ref import (
    ChartRef,
    ChartRefError,
    parse_ref,
    parse_selector,
)
from chart_manager.shared.events.store import get_event_store, query_events
from chart_manager.shared.events.writer import EventWriter

RefArgument = Annotated[
    str,
    typer.Argument(
        metavar="CHART@VERSION",
        help="The chart release this event is about, e.g. grafana@1.2.3.",
    ),
]


def _make_event_writer() -> EventWriter:
    """Build the lifecycle-event writer (module-level so tests can override)."""
    return EventWriter(source=container().settings.event_source, store=get_event_store)


def _parse_at(at: str | None) -> datetime | None:
    """Parse an --at ISO-8601 string into a UTC datetime.

    Normalized to UTC before the event is built: the store keeps timestamps
    as isoformat *strings*, and a `+02:00` stamp does not compare
    chronologically against the `+00:00` ones every live emitter writes.
    Naive input is rejected rather than assumed -- a backfill run from a
    laptop in another timezone silently shifting history is exactly the bug
    an explicit offset requirement prevents.
    """
    if at is None:
        return None
    try:
        ts = datetime.fromisoformat(at)
    except ValueError as exc:
        raise typer.BadParameter(f"invalid --at timestamp {at!r}: {exc}") from exc
    if ts.tzinfo is None:
        raise typer.BadParameter(
            f"--at timestamp {at!r} has no UTC offset; append one, "
            "e.g. 2026-07-30T12:00:00Z or 2026-07-30T14:00:00+02:00"
        )
    return ts.astimezone(UTC)


def _parse_ref(ref: str) -> ChartRef:
    """Parse `CHART@VERSION`; a malformed token is a usage error (exit 2)."""
    try:
        return parse_ref(ref)
    except ChartRefError as exc:
        raise typer.BadParameter(str(exc)) from exc


#: The composed event document, in `-o` form. Offered only with `--dry-run`
#: (a real emit's confirmation is narration, not data -- see `_emit`).
_DRY_RUN_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML)

DryRunOutputOption = Annotated[
    str | None,
    output_mod.output_option(*_DRY_RUN_OUTPUTS, extra_help=" Requires --dry-run."),
]

DryRunOption = Annotated[
    bool,
    typer.Option(
        "--dry-run",
        help=(
            "Print the fully-composed event document instead of writing it. "
            "Touches no backend; works with EVENTS_BACKEND unset."
        ),
    ),
]


def _emit_dry_run(document: dict[str, Any], *, ctx: typer.Context, output: str | None) -> None:
    """Print the document a real run would write, in the resolved `-o` form.

    The JSON round-trip is what makes the printout the *stored* shape:
    `to_dict` leaves phase enums and the images tuple as Python objects and
    lets the backend's encoder flatten them, so encoding here -- with the
    same stdlib encoder -- shows the values a reader of the ledger would
    see, and keeps `-o yaml` from choking on an Enum.
    """
    stored: dict[str, Any] = json.loads(json.dumps(document, default=str))
    mode = output_mod.resolve(output, ctx, allowed=_DRY_RUN_OUTPUTS, console=console)
    output_mod.emit(stored, mode=mode, table=_document_table(stored))


def _document_table(document: dict[str, Any]) -> Table:
    """One event document as Field/Value rows, leaves spelled as JSON."""
    table = Table("Field", "Value")
    for field, value in document.items():
        rendered = value if isinstance(value, str) else json.dumps(value)
        table.add_row(escape(field), escape(rendered))
    return table


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
    ctx: typer.Context,
    phase: Annotated[BuildPhase, typer.Option(help="Build lifecycle phase.")],
    ref: RefArgument,
    build_correlation_id: Annotated[str | None, typer.Option(help="Charts-repo PR.")] = None,
    pr_url: Annotated[str | None, typer.Option(help="PR URL")] = None,
    git_sha: Annotated[str | None, typer.Option(help="Charts-repo commit SHA.")] = None,
    at: Annotated[str | None, typer.Option(help="ISO-8601 event timestamp with an explicit UTC offset (naive is rejected; stored as UTC). Default: now. For backfill/seeding.")] = None,
    dry_run: DryRunOption = False,
    output: DryRunOutputOption = None,
    strict: Annotated[
        bool,
        typer.Option("--strict-events", help="Fail the step on emit error."),
    ] = False,
    ) -> None:
    """Emit a build-lifecycle event (charts repo CI)."""
    output_mod.require_dry_run(output, dry_run=dry_run)
    resolved = _parse_ref(ref)
    timestamp = _parse_at(at)
    if dry_run:
        event = _make_event_writer().compose_build(
            chart_name=resolved.name, chart_version=resolved.version, phase=phase,
            build_correlation_id=build_correlation_id, pr_url=pr_url, git_sha=git_sha,
            timestamp=timestamp,
        )
        _emit_dry_run(event.to_dict(), ctx=ctx, output=output)
        return
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
    ctx: typer.Context,
    environment: Annotated[str, typer.Option("--env", help="Target environment.")],
    phase: Annotated[PromotionPhase, typer.Option(help="Promotion lifecycle phase.")],
    ref: RefArgument,
    promotion_correlation_id: Annotated[str | None, typer.Option(help="Flux-repo PR.")] = None,
    build_correlation_id: Annotated[str | None, typer.Option(help="Originating charts-repo PR.")] = None,
    pr_url: Annotated[str | None, typer.Option(help="PR URL")] = None,
    git_sha: Annotated[str | None, typer.Option(help="Charts-repo commit SHA.")] = None,
    at: Annotated[str | None, typer.Option(help="ISO-8601 event timestamp with an explicit UTC offset (naive is rejected; stored as UTC). Default: now. For backfill/seeding.")] = None,
    dry_run: DryRunOption = False,
    output: DryRunOutputOption = None,
    strict: Annotated[
        bool,
        typer.Option("--strict-events", help="Fail the step on emit error."),
    ] = False,
    ) -> None:
    """Emit a promotion-lifecycle event (flux repo CI)."""
    output_mod.require_dry_run(output, dry_run=dry_run)
    resolved = _parse_ref(ref)
    timestamp = _parse_at(at)
    if dry_run:
        event = _make_event_writer().compose_promote(
            chart_name=resolved.name, chart_version=resolved.version,
            environment=environment, phase=phase,
            promotion_correlation_id=promotion_correlation_id,
            build_correlation_id=build_correlation_id, pr_url=pr_url, git_sha=git_sha,
            timestamp=timestamp,
        )
        _emit_dry_run(event.to_dict(), ctx=ctx, output=output)
        return
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


# --- the read side ---------------------------------------------------------

#: The listing renders as a table, or as the wire document from
#: `services/events/wire.py`.
_LIST_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML)

ListOutputOption = Annotated[str | None, output_mod.output_option(*_LIST_OUTPUTS)]

SelectorArgument = Annotated[
    str | None,
    typer.Argument(
        metavar="CHART[@VERSION]",
        help=(
            "Narrow to one chart's history, or to one release with "
            "CHART@VERSION. Absent: recent activity across every chart."
        ),
    ),
]

LimitOption = Annotated[
    int,
    typer.Option("--limit", "-n", min=1, help="Maximum events to show, newest first."),
]


def _query_events(request: EventQuery) -> list[dict[str, Any]]:
    """Run the read-side selection (module-level so tests can override)."""
    return query_events(request)


def list_events(
    ctx: typer.Context,
    selector: SelectorArgument = None,
    limit: LimitOption = DEFAULT_LIMIT,
    output: ListOutputOption = None,
) -> None:
    """List recent lifecycle events, newest first.

    Requires an events backend that can serve reads (EVENTS_BACKEND=cosmos
    today). The refusals are typed: `none`/unset says events are disabled
    and how to enable them; `dynamodb` says the read side is Cosmos-only.
    Both exit through the error's own outcome in the exit-code table --
    nothing the caller asked about failed, the environment has no readable
    ledger.
    """
    mode = output_mod.resolve(output, ctx, allowed=_LIST_OUTPUTS, console=console)
    try:
        parsed = None if selector is None else parse_selector(selector)
    except ChartRefError as exc:
        # A usage error, exactly as `_parse_ref` narrows it for emit.
        raise typer.BadParameter(str(exc)) from exc
    request = EventQuery.from_selector(parsed, limit=limit)
    try:
        events = _query_events(request)
    except EventReadError as exc:
        errors.print(f"[red]error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=exit_code_for(exc.outcome)) from exc
    if not events:
        # Narration, not data: an empty table (or a count:0 document) is the
        # projection; this line says the emptiness is real, not a bug.
        narration.print("no events matched")
    output_mod.emit(
        events_to_dict(events, query=request), mode=mode, table=_events_table(events)
    )


def _events_table(events: Sequence[dict[str, Any]]) -> Table:
    """Render recent activity for a human: one row per event, newest first."""
    table = Table("Chart", "Version", "Phase", "Env", "PR", "Source", "Timestamp", "Age")
    now = datetime.now(UTC)
    for event in events:
        table.add_row(
            escape(str(event.get("chart_name") or "?")),
            escape(str(event.get("chart_version") or "-")),
            escape(str(event.get("build_phase") or event.get("promotion_phase") or "-")),
            escape(str(event.get("environment") or "-")),
            _pr_link(event.get("pr_url")),
            escape(str(event.get("source") or "-")),
            _stamp(event.get("timestamp")),
            _age(event.get("timestamp"), now=now),
        )
    return table


def _pr_link(pr_url: Any) -> str:
    """The event's PR URL as a terminal hyperlink; "-" without one."""
    url = escape(str(pr_url or ""))
    return f"[link={url}]{url}[/link]" if url else "-"


def _stamp(timestamp: Any) -> str:
    """One ISO-8601 stamp normalized to compact UTC; "?" when unreadable."""
    try:
        parsed = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return "?"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _age(timestamp: Any, *, now: datetime) -> str:
    """Compact age of one ISO-8601 stamp: 42s, 5m, 3h, 12d; "?" when unreadable."""
    try:
        parsed = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return "?"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    seconds = max(0, int((now - parsed).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# --- the command tree ------------------------------------------------------
#
# Assembled here rather than in `cli/main.py` so the whole `event` group --
# emit and the read side -- is one file. `main.py` mounts it, the way it
# already mounts `upgrade` and `publish`.

emit_app = typer.Typer(no_args_is_help=True, help="Emit one platform lifecycle event.")
emit_app.command("build")(build)
emit_app.command("promote")(promote)

event_app = typer.Typer(no_args_is_help=True, help="Platform lifecycle events.")
event_app.add_typer(emit_app, name="emit")
event_app.command("list")(list_events)


def register(app: typer.Typer) -> None:
    """Mount the `event` group."""
    app.add_typer(event_app, name="event")


__all__ = ["build", "emit_app", "event_app", "list_events", "promote", "register"]
