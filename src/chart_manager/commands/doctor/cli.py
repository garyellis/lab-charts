"""`chart-manager doctor`: argument shape, projection and exit code; the checks are in `run`."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from chart_manager.cli import output as output_mod
from chart_manager.cli._container import container
from chart_manager.cli.streams import console, narration
from chart_manager.commands.doctor.models import DoctorReport
from chart_manager.commands.doctor.run import run
from chart_manager.plumbing.exit_codes import exit_code_for
from chart_manager.plumbing.preflight import CheckStatus

#: `doctor` produces a status table or a machine-readable document; there is
#: no yaml or markdown projection of a preflight to offer.
_DOCTOR_OUTPUTS = (output_mod.TABLE, output_mod.JSON)

OutputOption = Annotated[
    str | None,
    output_mod.output_option(output_mod.TABLE, output_mod.JSON),
]

#: Status -> the glyph and Rich style the table renders it with. A table so
#: the three statuses are styled in one place rather than at three branches.
_STATUS_STYLE: dict[CheckStatus, tuple[str, str]] = {
    CheckStatus.OK: ("ok", "green"),
    CheckStatus.FAILED: ("FAIL", "red"),
    CheckStatus.SKIPPED: ("skip", "dim"),
}


def doctor(
    ctx: typer.Context,
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
    invocation = container()
    report = run(
        settings=invocation.settings,
        runner=invocation.command_runner(),
        workspace=invocation.workspace,
    )

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
        narration.print(f"[green]all {len(report.checks)} checks passed[/green]")
        return
    names = ", ".join(check.name for check in failed)
    narration.print(f"[red]{len(failed)} of {len(report.checks)} checks failed:[/red] {names}")
