"""Run `plan`: select the work a change set calls for."""

from __future__ import annotations

from pathlib import Path

from chart_manager.commands import publish, test, validate
from chart_manager.commands.plan.models import PlannedRow, PlanOutcome, PlanRequest
from chart_manager.integrations.git import Git
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.shared.workspace import RepositoryWorkspace


def run(
    request: PlanRequest, *, workspace: RepositoryWorkspace, runner: CommandRunner
) -> PlanOutcome:
    """The validation rows, chart tests and charts to publish that the changed files select.

    Raises `SpecError` when `request.charts` names an unknown chart or one without chart
    tests, and `ExternalCommandError` when git cannot list the changed files.
    """
    if request.changes is None and (request.all_charts or request.charts):
        tests = test.select(None, workspace=workspace, charts=request.charts)
        return PlanOutcome(
            changed_files=(),
            validation=(),
            chart_tests=tests.tests,
            publish=(),
            spec_errors=tests.spec_errors,
        )
    changes = request.changes
    if changes is None:
        changes = tuple(Git(workspace.root, runner).changed_files(request.base))
    paths = tuple(sorted({Path(raw).as_posix() for raw in changes if raw}))
    validation = validate.select(paths, workspace=workspace)
    tests = test.select(paths, workspace=workspace)
    return PlanOutcome(
        changed_files=paths,
        validation=tuple(
            PlannedRow(
                row.chart,
                row.env,
                row.release,
                row.namespace,
                validation.reasons[(row.chart, row.env)],
            )
            for row in validation.rows
        ),
        chart_tests=tests.tests,
        publish=publish.select(paths, workspace=workspace),
        spec_errors=(*validation.spec_errors, *tests.spec_errors),
        warnings=validation.warnings,
    )
