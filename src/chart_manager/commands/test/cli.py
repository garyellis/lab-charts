"""`chart test` and `chart teardown`: flags, the dry-run plan, and exit status."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from chart_manager.api.v1alpha1.chart_lifecycle import DEFAULT_PROFILE
from chart_manager.cli import output as output_mod
from chart_manager.cli._container import container as _container
from chart_manager.cli._options import (
    ClusterNameOption,
    ProvisionHooksOption,
    provision_hooks_enabled,
)
from chart_manager.cli.streams import console, narration
from chart_manager.cli.streams import print_progress as _print_progress
from chart_manager.commands import test
from chart_manager.commands.test.models import LifecyclePlan
from chart_manager.commands.test.run import plan, run, teardown, teardown_plan
from chart_manager.commands.test.wire import plan_to_dict
from chart_manager.plumbing.commands import redact
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.settings import DEFAULT_CLUSTER_NAME
from chart_manager.shared.charts.chart import resolve_chart_target
from chart_manager.shared.workspace import RepositoryWorkspace

ProfileOption = Annotated[str, typer.Option("--profile", help="Chart-test profile.")]
NamespaceOverrideOption = Annotated[
    str | None,
    typer.Option(
        "--namespace",
        help="Override the namespace declared by the selected ChartLifecycle profile.",
    ),
]

_DRY_RUN_OUTPUTS = (output_mod.TABLE, output_mod.JSON, output_mod.YAML)

DryRunOutputOption = Annotated[
    str | None,
    output_mod.output_option(*_DRY_RUN_OUTPUTS, extra_help=" Requires --dry-run."),
]


def register(app: typer.Typer) -> None:
    """Attach `test` and `teardown` to the `chart` Typer group."""
    app.command("test")(chart_test)
    app.command("teardown")(chart_teardown)


def _target(chart: str) -> tuple[str, RepositoryWorkspace]:
    """The chart's name, and the workspace with its charts directory pointed at the chart's."""
    workspace = _container().workspace()
    target = resolve_chart_target(workspace, chart)
    charts_dir = target.path.parent.relative_to(workspace.root)
    return target.name, workspace.with_charts_dir(charts_dir)


def _render_test_plan(plan: LifecyclePlan, *, ctx: typer.Context, output: str | None) -> None:
    """Print the compiled chart-test plan; say on stderr what did not happen.

    The plan is what the caller asked for, so it is the projection and goes
    to stdout. That it was *only* a plan is narration, and stays off the
    stream a `-o json | jq` consumer reads.
    """
    mode = output_mod.resolve(output, ctx, allowed=_DRY_RUN_OUTPUTS, console=console)
    output_mod.emit(plan_to_dict(plan), mode=mode, table=_plan_table(plan))
    for warning in plan.warnings:
        narration.print(f"[yellow]warn:[/yellow] {escape(warning)}")
    narration.print(
        "[yellow]dry run[/yellow]: no cluster was created, nothing was installed or tested"
    )


def _plan_table(plan: LifecyclePlan) -> Table:
    table = Table("Step", "Action", "Chart", "Profile", "Namespace", "Release", "Command")
    for step, action in enumerate(plan.actions, start=1):
        table.add_row(
            str(step),
            action.kind.value,
            action.target.chart,
            action.target.profile or "",
            action.target.namespace or "",
            action.target.release or "",
            escape(redact(action.command)),
        )
    return table


def chart_test(
    ctx: typer.Context,
    chart: Annotated[
        str,
        typer.Argument(metavar="CHART", help="Chart name or chart directory."),
    ],
    profile: ProfileOption = DEFAULT_PROFILE,
    namespace: NamespaceOverrideOption = None,
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    dependent_tests: Annotated[
        bool,
        typer.Option(
            "--dependent-tests",
            help="Run chart tests affected by this chart.",
        ),
    ] = False,
    skip_requires: Annotated[
        bool,
        typer.Option(
            "--skip-requires",
            help=(
                "Reuse installed prerequisites without upgrading them; on a new cluster, "
                "install prerequisites but Helm-test selected targets only."
            ),
        ),
    ] = False,
    no_ensure_cluster: Annotated[
        bool,
        typer.Option("--no-ensure-cluster", help="Do not create the test cluster if missing."),
    ] = False,
    lint: Annotated[bool, typer.Option("--lint", help="Run helm lint before install.")] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the chart-test plan and exit; create no cluster, install nothing.",
        ),
    ] = False,
    output: DryRunOutputOption = None,
    run_provision_hooks: ProvisionHooksOption = None,
) -> None:
    """Install and exercise one chart on an ephemeral local Kubernetes cluster.

    `--dry-run` prints the compiled lifecycle plan -- every namespace,
    install and helm test the run would perform, in order -- and exits 0
    having touched nothing. It is the same plan object the real run
    executes, so it cannot describe work that would not happen.

    `-o` selects the form that plan is printed in. It is only meaningful
    with `--dry-run` -- a real run's report is progress narration, not a
    document -- so naming it without `--dry-run` is a usage error rather
    than a flag that is quietly ignored.
    """
    output_mod.require_dry_run(output, dry_run=dry_run)
    target, workspace = _target(chart)
    request = test.ChartTestRequest(
        chart=target,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        ensure_cluster=not no_ensure_cluster,
        include_dependent_tests=dependent_tests,
        skip_requires=skip_requires,
        lint=lint,
        run_provision_hooks=provision_hooks_enabled(run_provision_hooks),
    )
    if dry_run:
        _render_test_plan(plan(request, workspace=workspace), ctx=ctx, output=output)
        return
    container = _container()
    outcome = run(
        request,
        workspace=workspace,
        runner=container.command_runner(),
        settings=container.settings,
        progress=_print_progress,
    )
    if outcome.failed is not None:
        raise ChartManagerError(
            f"chart test failed at {outcome.failed.action_id}: {outcome.failed.detail}"
        )


def chart_teardown(
    chart: Annotated[
        str,
        typer.Argument(metavar="CHART", help="Chart name or chart directory."),
    ],
    profile: ProfileOption = DEFAULT_PROFILE,
    namespace: NamespaceOverrideOption = None,
    cluster_name: ClusterNameOption = DEFAULT_CLUSTER_NAME,
    dependent_tests: Annotated[
        bool,
        typer.Option("--dependent-tests", help="Include cleanups of affected chart tests."),
    ] = False,
    keep_cluster: Annotated[
        bool,
        typer.Option("--keep-cluster", help="Run cleanup hooks but keep the test cluster."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the cleanup hooks and exit; run nothing."),
    ] = False,
) -> None:
    """Run cleanup hooks in reverse install order, then delete the test cluster."""
    target, workspace = _target(chart)
    request = test.TeardownRequest(
        chart=target,
        profile=profile,
        namespace=namespace,
        cluster_name=cluster_name,
        include_dependent_tests=dependent_tests,
        keep_cluster=keep_cluster,
    )
    if dry_run:
        console.print(_plan_table(teardown_plan(request, workspace=workspace)))
        verb = "keep" if keep_cluster else "delete"
        narration.print(
            f"[yellow]dry run[/yellow]: ran no hook; would {verb} cluster {escape(cluster_name)}"
        )
        return
    container = _container()
    result = teardown(
        request,
        workspace=workspace,
        runner=container.command_runner(),
        settings=container.settings,
        progress=_print_progress,
    )
    if not result.ok:
        raise ChartManagerError(_teardown_failure(result))


def _teardown_failure(result: test.TeardownOutcome) -> str:
    problems = [
        f"cleanup {outcome.action_id} failed: {outcome.detail}"
        for outcome in result.failed_cleanups
    ]
    if result.delete_error is not None:
        problems.append(f"deleting cluster {result.cluster_name} failed: {result.delete_error}")
    return "chart teardown failed:\n" + "\n".join(problems)
