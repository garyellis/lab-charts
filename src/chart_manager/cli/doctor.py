"""`chart-manager doctor` -- the preflight surface.

Thin, and thin in a specific way that is worth stating because the obvious
implementation is not: **there is no check logic in this file.** No binary
name, no version flag, no "is the daemon up" heuristic. Each integration
owns its own preflight (`MY_COMMENTS.md`, and the design doc's P0 bullet);
`composition.Container.doctor_service` binds those to configured adapters;
`services/doctor.py` folds the results. What is left here is the three
things a surface owns: argument shape, projection, and the exit code.

The exit code is the interesting one. `doctor` is the second consumer of
`plumbing/exit_codes.py` after `cli/helmrelease.py`, and it consumes it the
same way: the layer below reports a semantic `Outcome`, this layer turns it
into a number with `exit_code_for`, and no integer literal appears in
between. That is what keeps "a missing binary is 127" a fact stated once,
in the table, rather than a convention re-implemented per command.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from chart_manager.cli import output as output_mod
from chart_manager.cli._container import container
from chart_manager.cli.streams import console, narration
from chart_manager.plumbing.exit_codes import exit_code_for
from chart_manager.plumbing.preflight import CheckStatus
from chart_manager.services.doctor import DoctorReport, DoctorService

#: `doctor` produces a status table or a machine-readable document; there is
#: no yaml or markdown projection of a preflight to offer.
_DOCTOR_OUTPUTS = (output_mod.TABLE, output_mod.JSON)

OutputOption = Annotated[
    str | None,
    output_mod.output_option(output_mod.TABLE, output_mod.JSON),
]

ForOption = Annotated[
    str | None,
    typer.Option(
        "--for",
        metavar="COMMAND",
        help=(
            "Only check what one command needs, e.g. --for 'chart validate'. "
            "Default: everything."
        ),
    ),
]

#: Status -> the glyph and Rich style the table renders it with. A table so
#: the three statuses are styled in one place rather than at three branches.
_STATUS_STYLE: dict[CheckStatus, tuple[str, str]] = {
    CheckStatus.OK: ("ok", "green"),
    CheckStatus.FAILED: ("FAIL", "red"),
    CheckStatus.SKIPPED: ("skip", "dim"),
}


def _make_doctor_service() -> DoctorService:
    """Build the default DoctorService (module-level so tests can override).

    Same seam as `cli/helmrelease.py::_make_promote_service`: adapter wiring
    lives in the composition root, and this function exists only so a test
    can inject fake providers without a real helm on the developer's PATH.
    """
    return container().doctor_service()


def register(app: typer.Typer) -> None:
    """Mount `doctor` onto the root app."""
    app.command("doctor")(doctor)


def doctor(
    ctx: typer.Context,
    for_: ForOption = None,
    output: OutputOption = None,
) -> None:
    """Check that the tools, kubecontext and backends this CLI needs are usable.

    Read-only and cluster-free: every probe either reads local state or asks
    one short, capped, non-mutating question of a remote. A cluster that is
    down, a docker daemon that is not running and an unreachable events
    backend are all *reported* -- `doctor` is the command you run when
    something is already broken, so it must not hang or crash on the
    breakage it exists to describe.

    Exit codes follow the table in `plumbing/exit_codes.py`: 127 when a
    required binary is not on PATH, 5 when the environment is at fault (no
    kubecontext, an unreachable backend), 3 when configuration is invalid,
    4 when a tool is installed but broken. Most fundamental failure wins.
    """
    mode = output_mod.resolve(output, ctx, allowed=_DOCTOR_OUTPUTS, console=console)
    service = _make_doctor_service()
    if for_ is not None and for_ not in service.commands():
        raise typer.BadParameter(
            f"unknown command: {for_} (known: {', '.join(service.commands())})",
            param_hint="--for",
        )

    report = service.run(for_command=for_)

    if mode == output_mod.JSON:
        typer.echo(json.dumps(report.to_dict(), indent=2))
    else:
        _render_table(report)

    if not report.ok:
        raise typer.Exit(code=exit_code_for(report.outcome))


def _render_table(report: DoctorReport) -> None:
    """Render the report for a human, with the fixes beside the failures.

    `remediation` gets its own column rather than a footnote because the
    reason to run a preflight is to be told what to do next, and a hint the
    operator has to scroll to find is one they will not read.
    """
    table = Table("Check", "Status", "Detail", "Fix")
    for check in report.checks:
        label, style = _STATUS_STYLE[check.status]
        table.add_row(
            check.name,
            f"[{style}]{label}[/{style}]",
            escape(check.detail),
            escape(check.remediation or ""),
        )
    console.print(table)
    _summarize(report)


def _summarize(report: DoctorReport) -> None:
    """One narration line saying whether the run passed.

    On stderr, not stdout: the table is the projection the caller asked for,
    and `chart-manager doctor | grep FAIL` must not also match a summary
    line. See `cli/streams.py`.
    """
    failed = [check for check in report.checks if check.status is CheckStatus.FAILED]
    if not failed:
        scope = "" if report.selector is None else f" for `{report.selector}`"
        narration.print(f"[green]all {len(report.checks)} checks passed{scope}[/green]")
        return
    names = ", ".join(check.name for check in failed)
    narration.print(f"[red]{len(failed)} of {len(report.checks)} checks failed:[/red] {names}")


__all__ = ["register"]
