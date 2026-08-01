"""`plan` -- "given a change set, what work is selected?".

One root-level command with output projections rather than one command per
consumer, which is what it replaced: a `ci cluster-test-matrix`, a
`ci publish-charts` and a `ci impact` that answered three views of the same
question and could not be asked in combination.

Two selection engines sit behind it and the split is not arbitrary --
explicit changed paths are what `LifecycleImpactService` analyses (with the
*reasons* a chart was selected, which is what `-o table` exists to show),
while `--base`/`--all`/`--chart` is a `MatrixSelection` that `CiService` owns.
Neither decides which charts anything; `_plan_cluster_tests` only decides
which service was asked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from chart_manager.cli import output as output_mod
from chart_manager.cli._options import RootOption
from chart_manager.cli._wiring import container as _container
from chart_manager.cli.streams import console
from chart_manager.composition import Settings
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
from chart_manager.services.ci import MatrixSelection
from chart_manager.services.ci_wire import cluster_test_matrix_to_dict
from chart_manager.services.lifecycle.impact import LifecycleImpactService

#: `plan`'s output vocabulary: the core three plus its own `github`, the
#: GitHub Actions matrix document. `github` stays command-local because it is
#: meaningless for `chart list` or `local status` -- see `cli/output.py`.
_PLAN_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML, output_mod.GITHUB)

#: The kinds of work a plan can cover. `all` is the default because the
#: question the command answers -- "given a change set, what work is
#: selected?" -- is normally asked about the whole pipeline.
_PLAN_KINDS = ("validate", "test", "publish", "all")

PlanOutputOption = Annotated[
    str | None,
    output_mod.output_option(*_PLAN_OUTPUTS, extra_help=" github is the GHA matrix JSON."),
]

PlanForOption = Annotated[
    str,
    typer.Option(
        "--for",
        help="Work kind to plan: validate, test, publish, or all.",
        callback=lambda value: output_mod.choice(value, _PLAN_KINDS, param_hint="--for"),
    ),
]


def register(app: typer.Typer) -> None:
    """Mount `plan` onto the root app.

    Root-level rather than under a group: it is asked about the repository,
    not about one chart or one cluster.
    """
    app.command("plan")(plan)


def _impact_service(root: Path) -> LifecycleImpactService:
    """Build the impact service at a seam tests can replace."""
    settings = Settings()
    return LifecycleImpactService(
        root,
        charts_dir=settings.charts_dir,
        local_config=settings.local_config,
    )


def _changed_paths(
    changed_files: Path | None,
    changed_file: list[str],
) -> list[str]:
    changes: list[str] = []
    if changed_files is not None:
        try:
            contents = changed_files.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise typer.BadParameter(
                f"cannot read changed-files input {changed_files}: {exc}",
                param_hint="--changed-files",
            ) from exc
        changes.extend(line.strip() for line in contents.splitlines() if line.strip())
    changes.extend(path.strip() for path in changed_file if path.strip())
    if not changes:
        raise typer.BadParameter(
            "provide at least one changed path via --changed-files or --changed-file",
            param_hint="--changed-files / --changed-file",
        )
    return changes


def _render_impact_text(
    result: Any,
    *,
    validation: bool = True,
    cluster_tests: bool = True,
) -> None:
    """Render the impact document as text, optionally narrowed to one section.

    `validation`/`cluster_tests` implement `plan --for`. Warnings and spec
    errors are printed for every selection deliberately: a warning is usually
    the answer to "why is nothing selected?", which is the question a narrowed
    view is most often asked.
    """
    if validation:
        typer.echo("Validation:")
        if not result.validation:
            typer.echo("  none")
        for case in result.validation:
            typer.echo(f"  {case.chart}/{case.environment}")
            for reason in case.reasons:
                typer.echo(
                    f"    - {reason.code}: {reason.changed_file.as_posix()} — {reason.detail}"
                )

    if cluster_tests:
        typer.echo("Cluster tests:")
        if not result.cluster_tests:
            typer.echo("  none")
        for case in result.cluster_tests:
            typer.echo(f"  {case.chart}/{case.profile}")
            for reason in case.reasons:
                typer.echo(
                    f"    - {reason.code}: {reason.changed_file.as_posix()} — {reason.detail}"
                )

    if result.warnings:
        typer.echo("Warnings:")
        for warning in result.warnings:
            typer.echo(f"  - {warning}")
    if result.spec_errors:
        typer.echo("Spec errors:")
        for error in result.spec_errors:
            typer.echo(f"  - {error}")


def _plan_cluster_tests(
    root: Path,
    *,
    base: str,
    all_charts: bool,
    charts: list[str] | None,
    changed_files: Path | None,
    changed_file: list[str],
) -> tuple[Any, ...]:
    """Select cluster-test entries from whichever change source was given.

    Two engines, because the two change sources are genuinely different
    questions and each already has a service that answers it:

      * explicit paths (`--changed-files`/`--changed-file`) are what the
        lifecycle impact service analyses, and its `cluster_tests` are the
        same `ClusterTestImpact` values the matrix is built from;
      * `--base`/`--all`/`--chart` is a `MatrixSelection`, and
        `CiService.matrix` owns the `all` > `--chart` > diff precedence.

    Neither branch decides *which charts* anything -- that stays in
    `services/`. This function only decides which service was asked.
    """
    if changed_files is not None or changed_file:
        paths = _changed_paths(changed_files, changed_file)
        return tuple(_impact_service(root).analyze(paths).cluster_tests)
    selection = MatrixSelection(
        base=base,
        all_charts=all_charts,
        charts=tuple(charts or ()),
    )
    return tuple(_container().ci_service(root).matrix(selection))


def plan(
    ctx: typer.Context,
    base: Annotated[str, typer.Option("--base", help="Git comparison base.")] = "origin/main",
    changed_files: Annotated[
        Path | None,
        typer.Option(
            "--changed-files",
            help="Path to a newline-delimited changed-file list.",
        ),
    ] = None,
    changed_file: Annotated[
        list[str],
        typer.Option(
            "--changed-file",
            help="Changed repository-relative path (repeatable).",
        ),
    ] = [],
    all_charts: Annotated[
        bool,
        typer.Option("--all", help="Include every chart with enabled cluster tests."),
    ] = False,
    charts: Annotated[
        list[str] | None,
        typer.Option(
            "--chart",
            help="Explicit chart to include; repeat for multiple charts.",
        ),
    ] = None,
    for_: PlanForOption = "all",
    output: PlanOutputOption = None,
    root: RootOption = Path("."),
) -> None:
    """Answer "given a change set, what work is selected?".

    One command with output projections rather than one command per consumer.
    `-o github` is the GitHub Actions matrix a workflow feeds to
    `strategy.matrix`; `-o table` is the same selection with the *reasons*
    kept, which is what answers "why is this chart in my matrix?" and, via
    warnings, "why is nothing selected?"; `-o json`/`-o yaml` are the machine
    document.

    `--for` narrows the work kind. It selects the engine for `publish` (a
    lexical ownership projection that must not inherit lifecycle fanout) and
    narrows the `table` view for `validate`/`test`. `-o json`/`-o yaml`
    deliberately emit the *complete* impact document whatever `--for` says:
    the payload is a versioned wire contract owned by `services/`, and
    deleting a key from it here would fork that contract in the surface.

    The output default is `auto`, so a bare `plan` in a pipe emits `json`
    rather than the table a terminal gets. `--for publish -o table` is the
    newline-delimited chart list `.github/workflows/ci.yaml` captures, and it
    names `-o table` explicitly for exactly that reason.
    """
    output = output_mod.resolve(output, ctx, allowed=_PLAN_OUTPUTS, console=console)
    if output == "github" and for_ in {"validate", "publish"}:
        raise typer.BadParameter(
            f"-o github is the cluster-test matrix, which has no '{for_}' projection",
            param_hint="--for",
        )
    # Flag exclusivity is the one part of this that is genuinely the
    # surface's: it classifies how the caller was *invoked*. Which selector
    # runs, and what shape the answer takes, belong to services/.
    if all_charts and charts:
        raise ChartManagerError("--all and --chart are mutually exclusive")

    if for_ == "publish":
        # Direct ownership only, from an explicit list. `--changed-file` is
        # not accepted because the service reads the file itself, and
        # publishing must not inherit lifecycle or dependency fanout.
        if changed_files is None:
            raise typer.BadParameter(
                "planning publish work needs an explicit changed-file list",
                param_hint="--changed-files",
            )
        selected = _container().ci_service(root).directly_changed_charts(changed_files)
        if output == output_mod.TABLE:
            for chart in selected:
                console.print(chart)
        else:
            output_mod.emit(selected, mode=output)
        return

    if output == "github":
        entries = _plan_cluster_tests(
            root,
            base=base,
            all_charts=all_charts,
            charts=charts,
            changed_files=changed_files,
            changed_file=changed_file,
        )
        payload = cluster_test_matrix_to_dict(entries)
        # Compact separators and sorted keys because this lands in a shell
        # variable in `.github/workflows/ci.yaml`, not in a human's terminal.
        typer.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return

    # table/json/yaml are projections of the impact document, which only the
    # lifecycle impact service produces, and only from explicit paths.
    # `_changed_paths` raises the "provide at least one changed path" usage
    # error when neither source was given.
    result = _impact_service(root).analyze(_changed_paths(changed_files, changed_file))
    if output != output_mod.TABLE:
        output_mod.emit(result.to_dict(), mode=output)
    else:
        _render_impact_text(
            result,
            validation=for_ in {"validate", "all"},
            cluster_tests=for_ in {"test", "all"},
        )
    if result.spec_errors:
        # `spec_errors` is by construction a list of unparseable authored
        # lifecycle files, so this is design §6.1's spec error -- exit 3, not
        # the generic 1 it used to be. The impact document is still printed;
        # what failed is the input to it.
        raise typer.Exit(code=exit_code_for(Outcome.SPEC))


__all__ = ["register"]
