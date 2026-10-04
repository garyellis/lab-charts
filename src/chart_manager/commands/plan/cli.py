"""`plan`: flags, the four output modes and exit status.

Each output mode reads its changes its own way:
- `table`, `json` and `yaml` need explicit paths (`--changed-files`, `--changed-file`);
- `-o github` takes explicit paths, else `--all`, `--chart` or the diff against `--base`;
- `--for publish` takes only `--changed-files`.

`.github/workflows/ci.yaml` reads two of them: the `-o github` matrix and the
`--for publish -o table` chart list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from chart_manager.cli import output as output_mod
from chart_manager.cli._container import container as _container
from chart_manager.cli.streams import console
from chart_manager.commands import test, validate
from chart_manager.commands.plan.models import PlanOutcome, PlanRequest
from chart_manager.commands.plan.run import run
from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
from chart_manager.services.publish import directly_changed_charts

_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML, output_mod.GITHUB)
_KINDS = ("validate", "test", "publish", "all")


def register(app: typer.Typer) -> None:
    """Attach `plan` to the root app."""
    app.command("plan")(plan)


def plan(
    ctx: typer.Context,
    base: Annotated[str, typer.Option("--base", help="Git comparison base.")] = "origin/main",
    changed_files: Annotated[
        Path | None,
        typer.Option(
            "--changed-files",
            help=(
                "Path to a newline-delimited changed-file list; paths are relative to "
                "the workspace root."
            ),
        ),
    ] = None,
    changed_file: Annotated[
        list[str],
        typer.Option("--changed-file", help="Changed repository-relative path (repeatable)."),
    ] = [],
    all_charts: Annotated[
        bool, typer.Option("--all", help="Include every chart with enabled chart tests.")
    ] = False,
    charts: Annotated[
        list[str] | None,
        typer.Option("--chart", help="Explicit chart to include; repeat for multiple charts."),
    ] = None,
    for_: Annotated[
        str,
        typer.Option(
            "--for",
            help="Work kind to plan: validate, test, publish, or all.",
            callback=lambda value: output_mod.choice(value, _KINDS, param_hint="--for"),
        ),
    ] = "all",
    output: Annotated[
        str | None,
        output_mod.output_option(*_OUTPUTS, extra_help=" github is the GHA matrix JSON."),
    ] = None,
) -> None:
    """Show the validation rows, chart tests and charts to publish that a change set selects.

    `-o table` shows why each was selected and narrows to `--for`; `-o json`/`-o yaml` always
    emit the whole document. Spec errors exit 3. The output default is `auto`, so CI names
    `-o table` for the publish list.
    """
    mode = output_mod.resolve(output, ctx, allowed=_OUTPUTS, console=console)
    container = _container()
    workspace = container.workspace()
    if mode == output_mod.GITHUB and for_ in {"validate", "publish"}:
        raise typer.BadParameter(
            f"-o github is the chart-test matrix, which has no '{for_}' projection",
            param_hint="--for",
        )
    if all_charts and charts:
        raise ChartManagerError("--all and --chart are mutually exclusive")

    if for_ == "publish":
        if changed_files is None:
            raise typer.BadParameter(
                "planning publish work needs an explicit changed-file list",
                param_hint="--changed-files",
            )
        selected = directly_changed_charts(workspace, _read_changed_files(changed_files))
        if mode == output_mod.TABLE:
            for chart in selected:
                console.print(chart)
        else:
            output_mod.emit(selected, mode=mode)
        return

    explicit = changed_files is not None or bool(changed_file)
    if mode == output_mod.GITHUB:
        request = PlanRequest(
            changes=_changed_paths(changed_files, changed_file) if explicit else None,
            base=base,
            all_charts=all_charts,
            charts=tuple(charts or ()),
        )
        outcome = run(request, workspace=workspace, runner=container.command_runner())
        # Spec errors fail only the git-diff matrix.
        if request.changes is None and outcome.spec_errors:
            detail = "\n".join(f"- {error}" for error in outcome.spec_errors)
            raise SpecError(f"lifecycle impact analysis found spec errors:\n{detail}")
        include = [{"chart": t.chart, "profile": t.profile} for t in outcome.chart_tests.tests]
        typer.echo(json.dumps({"include": include}, separators=(",", ":"), sort_keys=True))
        return

    request = PlanRequest(changes=_changed_paths(changed_files, changed_file))
    outcome = run(request, workspace=workspace, runner=container.command_runner())
    if mode == output_mod.TABLE:
        _print_table(outcome, for_)
    else:
        output_mod.emit(_to_dict(outcome), mode=mode)
    if outcome.spec_errors:
        raise typer.Exit(code=exit_code_for(Outcome.SPEC))


def _changed_paths(changed_files: Path | None, changed_file: list[str]) -> tuple[str, ...]:
    """The non-blank paths in `--changed-files` and `--changed-file`; at least one."""
    changes = _read_changed_files(changed_files) if changed_files is not None else []
    changes.extend(path.strip() for path in changed_file if path.strip())
    if not changes:
        raise typer.BadParameter(
            "provide at least one changed path via --changed-files or --changed-file",
            param_hint="--changed-files / --changed-file",
        )
    return tuple(changes)


def _read_changed_files(changed_files: Path) -> list[str]:
    """The non-blank paths in `--changed-files`; an unreadable file is a usage error."""
    try:
        contents = changed_files.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise typer.BadParameter(
            f"cannot read changed-files input {changed_files}: {exc}",
            param_hint="--changed-files",
        ) from exc
    return [line.strip() for line in contents.splitlines() if line.strip()]


def _print_table(outcome: PlanOutcome, for_: str) -> None:
    """Each selected row and chart test with its reasons; warnings and spec errors always."""
    if for_ in {"validate", "all"}:
        typer.echo("Validation:")
        if not outcome.validation.rows:
            typer.echo("  none")
        for row in outcome.validation.rows:
            typer.echo(f"  {row.chart}/{row.env}")
            _print_reasons(outcome.validation.reasons[(row.chart, row.env)])
    if for_ in {"test", "all"}:
        typer.echo("Chart tests:")
        if not outcome.chart_tests.tests:
            typer.echo("  none")
        for entry in outcome.chart_tests.tests:
            typer.echo(f"  {entry.chart}/{entry.profile}")
            _print_reasons(entry.reasons)
    if outcome.validation.warnings:
        typer.echo("Warnings:")
        for warning in outcome.validation.warnings:
            typer.echo(f"  - {warning}")
    if outcome.spec_errors:
        typer.echo("Spec errors:")
        for error in outcome.spec_errors:
            typer.echo(f"  - {error}")


def _print_reasons(reasons: tuple[validate.Reason, ...] | tuple[test.Reason, ...]) -> None:
    for reason in reasons:
        typer.echo(f"    - {reason.code}: {reason.changed_file.as_posix()} — {reason.detail}")


def _to_dict(outcome: PlanOutcome) -> dict[str, Any]:
    """The JSON/YAML document."""
    return {
        "changed_files": list(outcome.changed_files),
        "validation": [
            {
                "chart": row.chart,
                "environment": row.env,
                "release": row.release,
                "namespace": row.namespace,
                "reasons": _reasons(outcome.validation.reasons[(row.chart, row.env)]),
            }
            for row in outcome.validation.rows
        ],
        "chart_tests": [
            {"chart": entry.chart, "profile": entry.profile, "reasons": _reasons(entry.reasons)}
            for entry in outcome.chart_tests.tests
        ],
        "publish": list(outcome.publish),
        "spec_errors": list(outcome.spec_errors),
        "warnings": list(outcome.validation.warnings),
    }


def _reasons(reasons: tuple[validate.Reason, ...] | tuple[test.Reason, ...]) -> list[dict[str, str]]:
    return [
        {
            "code": reason.code.value,
            "changed_file": reason.changed_file.as_posix(),
            "detail": reason.detail,
        }
        for reason in reasons
    ]
