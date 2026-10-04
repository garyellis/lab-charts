"""Run `plan`: select the work a change set calls for."""

from __future__ import annotations

from pathlib import Path

from chart_manager.commands import publish, test, validate
from chart_manager.commands.plan.models import PlanOutcome, PlanRequest
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
        return PlanOutcome((), validate.Selection(rows=()), tests, ())
    changes = request.changes
    if changes is None:
        changes = tuple(Git(workspace.root, runner).changed_files(request.base))
    paths = tuple(sorted({Path(raw).as_posix() for raw in changes if raw}))
    return PlanOutcome(
        changed_files=paths,
        validation=validate.select(paths, workspace=workspace),
        chart_tests=test.select(paths, workspace=workspace),
        publish=publish.select(paths, workspace=workspace),
    )
