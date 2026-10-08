"""`plan.run()`: the validation rows, chart tests and charts to publish that changes select."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.commands import plan
from chart_manager.commands.plan.run import run
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.yaml_files import dump_yaml, parse_yaml
from chart_manager.shared.workspace import RepositoryWorkspace
from tests.conftest import FakeCommandRunner, MakeChart, argv_prefix, plain_argv, workspace_for


def _workspace(root: Path) -> RepositoryWorkspace:
    return workspace_for(
        root,
        fanout={"validation": ["validation-tool/**"], "chartTest": ["kind-config.yaml"]},
    )


def _run(root: Path, request: plan.PlanRequest, runner: FakeCommandRunner | None = None):  # type: ignore[no-untyped-def]
    return run(request, workspace=_workspace(root), runner=runner or FakeCommandRunner())


def _with_validation(chart: Path, *envs: str) -> None:
    path = chart / "chart-lifecycle.yaml"
    lifecycle = parse_yaml(path.read_text())
    lifecycle["spec"]["validation"] = {
        "releaseName": chart.name,
        "namespaceTemplate": "lab-${env}",
        "environments": {env: {"values": ["values.yaml"]} for env in envs},
        "triggers": {"values.yaml": list(envs)},
    }
    path.write_text(dump_yaml(lifecycle))


def _rows(outcome: plan.PlanOutcome) -> list[tuple[str, str]]:
    return [(row.chart, row.env) for row in outcome.validation.rows]


def _tests(outcome: plan.PlanOutcome) -> list[tuple[str, str]]:
    return [(entry.chart, entry.profile) for entry in outcome.chart_tests.tests]


def test_a_chart_change_selects_its_validation_its_chart_tests_and_its_publish(
    chart_root: Path, make_chart: MakeChart
) -> None:
    _with_validation(make_chart("source", dependent_tests=[("consumer", "full")]), "dev", "prod")
    make_chart("consumer", profiles={"minimal": {}, "full": {}})

    outcome = _run(chart_root, plan.PlanRequest(changes=("charts/source/values.yaml",)))

    assert outcome.changed_files == ("charts/source/values.yaml",)
    assert _rows(outcome) == [("source", "dev"), ("source", "prod")]
    assert [r.code for r in outcome.validation.reasons[("source", "dev")]] == [
        "validation-trigger"
    ]
    assert _tests(outcome) == [("consumer", "full"), ("source", "minimal")]
    assert outcome.publish == ("source",)
    assert outcome.spec_errors == ()


def test_changed_files_are_deduplicated_and_sorted(chart_root: Path, make_chart: MakeChart) -> None:
    make_chart("app")

    outcome = _run(
        chart_root,
        plan.PlanRequest(changes=("charts/app/values.yaml", "charts/app/values.yaml", "README.md")),
    )

    assert outcome.changed_files == ("README.md", "charts/app/values.yaml")
    assert _tests(outcome) == [("app", "minimal")]


def test_an_unrelated_change_selects_nothing(chart_root: Path, make_chart: MakeChart) -> None:
    _with_validation(make_chart("app"), "dev")

    outcome = _run(chart_root, plan.PlanRequest(changes=("docs/architecture.md",)))

    assert (_rows(outcome), _tests(outcome), outcome.publish) == ([], [], ())


def test_the_validation_and_chart_test_fanouts_are_independent(
    chart_root: Path, make_chart: MakeChart
) -> None:
    _with_validation(make_chart("app"), "dev")

    validation_only = _run(chart_root, plan.PlanRequest(changes=("validation-tool/x.yaml",)))
    chart_tests_only = _run(chart_root, plan.PlanRequest(changes=("kind-config.yaml",)))

    assert (_rows(validation_only), _tests(validation_only)) == ([("app", "dev")], [])
    assert (_rows(chart_tests_only), _tests(chart_tests_only)) == ([], [("app", "minimal")])


def test_a_workspace_file_change_selects_every_validation_row_and_chart_test(
    chart_root: Path, make_chart: MakeChart
) -> None:
    _with_validation(make_chart("alpha"), "dev", "prod")
    _with_validation(make_chart("beta"), "dev")

    outcome = _run(chart_root, plan.PlanRequest(changes=(".chart-manager/workspace.yaml",)))

    assert _rows(outcome) == [("alpha", "dev"), ("alpha", "prod"), ("beta", "dev")]
    assert _tests(outcome) == [("alpha", "minimal"), ("beta", "minimal")]
    assert outcome.publish == ()


def test_chart_test_spec_errors_reach_the_plan_outcome(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("source", dependent_tests=[("source", "nope")])

    outcome = _run(chart_root, plan.PlanRequest(changes=("charts/source/values.yaml",)))

    assert outcome.spec_errors == (
        "source dependentTests source:nope: unknown profile 'nope'. available profiles: minimal",
    )


def test_without_changes_the_git_diff_against_base_is_planned(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha")
    make_chart("beta")
    runner = FakeCommandRunner().respond(
        argv_prefix("git", "diff"), stdout="charts/beta/values.yaml\n\n"
    )

    outcome = _run(chart_root, plan.PlanRequest(base="merge-base"), runner)

    assert ("git", "diff", "--name-only", "--relative", "merge-base...HEAD") in [
        plain_argv(call) for call in runner.calls
    ]
    assert outcome.changed_files == ("charts/beta/values.yaml",)
    assert (_tests(outcome), outcome.publish) == ([("beta", "minimal")], ("beta",))


def test_a_git_failure_is_raised(chart_root: Path) -> None:
    runner = FakeCommandRunner().respond(argv_prefix("git", "rev-parse"), returncode=128)

    with pytest.raises(ExternalCommandError, match="not a git repository"):
        _run(chart_root, plan.PlanRequest(), runner)


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        (plan.PlanRequest(all_charts=True), [("alpha", "full"), ("beta", "minimal")]),
        (plan.PlanRequest(charts=("beta",)), [("beta", "minimal")]),
    ],
    ids=["all", "charts"],
)
def test_all_charts_or_named_charts_pick_chart_tests_without_git(
    chart_root: Path, make_chart: MakeChart, request_: plan.PlanRequest, expected: list
) -> None:
    make_chart("alpha", profiles={"smoke": {}, "full": {}})
    make_chart("beta")
    runner = FakeCommandRunner(when_exhausted="raise")

    outcome = _run(chart_root, request_, runner)

    assert _tests(outcome) == expected
    assert (outcome.changed_files, _rows(outcome), outcome.publish) == ((), [], ())
    assert runner.calls == []
