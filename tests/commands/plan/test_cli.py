"""`plan` at the CLI: flags, output modes and exit codes, with `run()` faked.

`.github/workflows/ci.yaml` reads the `-o github` matrix and the `--for publish -o table`
chart list, so those two are pinned byte for byte.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_manager.commands import plan, test, validate
from chart_manager.commands.plan import cli as plan_cli
from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.exit_codes import EXIT_SPEC
from chart_manager.plumbing.yaml_files import parse_yaml
from tests.conftest import MakeChart, cli

CHANGED = Path("charts/grafana/values-dev.yaml")


def outcome_with(*, spec_errors: tuple[str, ...] = (), warnings: tuple[str, ...] = ()) -> plan.PlanOutcome:
    """grafana/dev to validate, grafana/minimal to test and grafana to publish."""
    reason = validate.Reason(validate.ReasonCode.VALIDATION_TRIGGER, CHANGED, "triggered")
    return plan.PlanOutcome(
        changed_files=(CHANGED.as_posix(),),
        validation=validate.Selection(
            rows=(validate.Row("grafana", "dev", "grafana", "lab-dev", {}),),
            reasons={("grafana", "dev"): (reason,)},
            spec_errors=spec_errors,
            warnings=warnings,
        ),
        chart_tests=test.Selection(
            (
                test.SelectedTest(
                    "grafana",
                    "minimal",
                    (test.Reason(test.ReasonCode.CHART_CHANGE, CHANGED, "grafana changed"),),
                ),
                test.SelectedTest("loki", "full"),
            )
        ),
        publish=("grafana",),
    )


class FakeRun:
    """Stands in for `plan.run`: records each request and answers with `result`."""

    def __init__(self) -> None:
        self.requests: list[plan.PlanRequest] = []
        self.result = outcome_with()

    def __call__(self, request: plan.PlanRequest, **_: object) -> plan.PlanOutcome:
        self.requests.append(request)
        return self.result


@pytest.fixture
def fake_run(chart_root: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    monkeypatch.chdir(chart_root)
    fake = FakeRun()
    monkeypatch.setattr(plan_cli, "run", fake)
    return fake


# --- -o github: the chart-test matrix CI reads -------------------------------


@pytest.mark.parametrize(
    ("argv", "request_"),
    [
        (["--all"], plan.PlanRequest(all_charts=True)),
        (["--chart", "loki", "--chart", "grafana"], plan.PlanRequest(charts=("loki", "grafana"))),
        (["--base", "abc123"], plan.PlanRequest(base="abc123")),
    ],
    ids=["all", "charts", "base"],
)
def test_github_prints_the_compact_matrix_ci_reads(
    fake_run: FakeRun, argv: list[str], request_: plan.PlanRequest
) -> None:
    result = cli("plan", "-o", "github", *argv)

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        '{"include":[{"chart":"grafana","profile":"minimal"},{"chart":"loki","profile":"full"}]}\n'
    )
    assert fake_run.requests == [request_]


def test_github_prints_an_empty_include_when_nothing_is_selected(fake_run: FakeRun) -> None:
    fake_run.result = plan.PlanOutcome((), validate.Selection(rows=()), test.Selection(()), ())

    result = cli("plan", "-o", "github", "--base", "abc123")

    assert (result.exit_code, result.stdout) == (0, '{"include":[]}\n')


def test_github_fails_on_spec_errors_only_for_the_git_diff(fake_run: FakeRun) -> None:
    fake_run.result = outcome_with(spec_errors=("bad: invalid ChartLifecycle",))

    from_git = cli("plan", "-o", "github", "--base", "abc123")
    explicit = cli("plan", "-o", "github", "--changed-file", CHANGED.as_posix())

    assert isinstance(from_git.exception, SpecError)  # main() exits 3
    assert "bad: invalid ChartLifecycle" in str(from_git.exception)
    assert explicit.exit_code == 0
    assert fake_run.requests[1] == plan.PlanRequest(changes=(CHANGED.as_posix(),))


@pytest.mark.parametrize("kind", ["validate", "publish"])
def test_github_rejects_a_work_kind_it_has_no_matrix_for(fake_run: FakeRun, kind: str) -> None:
    result = cli("plan", "--all", "--for", kind, "-o", "github")

    assert result.exit_code == 2
    assert "--for" in result.stderr
    assert fake_run.requests == []


def test_all_and_chart_are_mutually_exclusive(fake_run: FakeRun) -> None:
    result = cli("plan", "--all", "--chart", "alpha", "-o", "github")

    assert result.exit_code == 1
    assert "mutually exclusive" in str(result.exception)
    assert fake_run.requests == []


# --- --for publish: the chart list CI reads ----------------------------------


def test_publish_table_prints_one_changed_chart_per_line(
    fake_run: FakeRun, chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha")
    make_chart("zeta")
    changed = chart_root / "changed.txt"
    changed.write_text(
        "charts/zeta/values.yaml\n\n  charts/alpha/Chart.yaml  \ncharts/gone/x\nkind-config.yaml\n"
    )

    result = cli("plan", "--for", "publish", "-o", "table", "--changed-files", str(changed))

    assert (result.exit_code, result.stdout) == (0, "alpha\nzeta\n")
    assert fake_run.requests == []


@pytest.mark.parametrize(("output", "load"), [("json", json.loads), ("yaml", parse_yaml)])
def test_publish_machine_output_is_a_bare_list(
    fake_run: FakeRun, chart_root: Path, make_chart: MakeChart, output: str, load: object
) -> None:
    make_chart("alpha")
    changed = chart_root / "changed.txt"
    changed.write_text("charts/alpha/values.yaml\n")

    result = cli("plan", "--for", "publish", "-o", output, "--changed-files", str(changed))

    assert result.exit_code == 0
    assert load(result.stdout) == ["alpha"]  # type: ignore[operator]


def test_publish_needs_a_changed_files_list(fake_run: FakeRun) -> None:
    result = cli("plan", "--for", "publish", "--changed-file", "charts/alpha/values.yaml")

    assert result.exit_code == 2
    assert "--changed-files" in result.stderr


def test_publish_with_an_unreadable_list_is_a_spec_error(fake_run: FakeRun, tmp_path: Path) -> None:
    result = cli("plan", "--for", "publish", "--changed-files", str(tmp_path / "missing.txt"))

    assert isinstance(result.exception, SpecError)  # main() exits 3
    assert "cannot read changed-files input" in str(result.exception)


# --- table, json, yaml: explicit paths only ----------------------------------


def test_both_changed_file_sources_become_the_request(fake_run: FakeRun, tmp_path: Path) -> None:
    changed = tmp_path / "changes.txt"
    changed.write_text("\n charts/grafana/values-dev.yaml \n\n")

    result = cli(
        "plan", "--changed-files", str(changed), "--changed-file", "kind-config.yaml",
        "--changed-file", " ", "-o", "json",
    )

    assert result.exit_code == 0
    assert fake_run.requests == [
        plan.PlanRequest(changes=("charts/grafana/values-dev.yaml", "kind-config.yaml"))
    ]


@pytest.mark.parametrize(
    "argv", [["--base", "abc123"], ["--changed-files", "missing.txt"]], ids=["none", "unreadable"]
)
def test_table_needs_readable_explicit_paths(fake_run: FakeRun, argv: list[str]) -> None:
    result = cli("plan", "-o", "table", *argv)

    assert result.exit_code == 2
    assert "--changed-files" in result.stderr
    assert fake_run.requests == []


def test_an_unknown_output_is_rejected_before_planning(fake_run: FakeRun) -> None:
    result = cli("plan", "--changed-file", "kind-config.yaml", "-o", "toml")

    assert result.exit_code == 2
    assert "table, json, yaml, github" in result.stderr
    assert fake_run.requests == []


def test_table_shows_reasons_warnings_and_spec_errors_and_exits_3(fake_run: FakeRun) -> None:
    fake_run.result = outcome_with(spec_errors=("grafana: bad",), warnings=("unmatched x",))

    result = cli("plan", "-o", "table", "--changed-file", CHANGED.as_posix())

    assert result.exit_code == EXIT_SPEC
    assert result.stdout == (
        "Validation:\n"
        "  grafana/dev\n"
        "    - validation-trigger: charts/grafana/values-dev.yaml — triggered\n"
        "Chart tests:\n"
        "  grafana/minimal\n"
        "    - chart-change: charts/grafana/values-dev.yaml — grafana changed\n"
        "  loki/full\n"
        "Warnings:\n"
        "  - unmatched x\n"
        "Spec errors:\n"
        "  - grafana: bad\n"
    )


@pytest.mark.parametrize(
    ("kind", "present", "absent"),
    [("validate", "Validation:", "Chart tests:"), ("test", "Chart tests:", "Validation:")],
)
def test_for_narrows_the_table(fake_run: FakeRun, kind: str, present: str, absent: str) -> None:
    fake_run.result = outcome_with(warnings=("unmatched x",))

    result = cli("plan", "-o", "table", "--for", kind, "--changed-file", CHANGED.as_posix())

    assert result.exit_code == 0
    assert present in result.stdout
    assert absent not in result.stdout
    assert "unmatched x" in result.stdout


@pytest.mark.parametrize(("output", "load"), [("json", json.loads), ("yaml", parse_yaml)])
def test_machine_output_is_the_whole_document_whatever_for_says(
    fake_run: FakeRun, output: str, load: object
) -> None:
    result = cli(
        "plan", "-o", output, "--for", "validate", "--changed-file", CHANGED.as_posix()
    )

    assert result.exit_code == 0
    assert load(result.stdout) == {  # type: ignore[operator]
        "changed_files": ["charts/grafana/values-dev.yaml"],
        "validation": [
            {
                "chart": "grafana",
                "environment": "dev",
                "release": "grafana",
                "namespace": "lab-dev",
                "reasons": [
                    {
                        "code": "validation-trigger",
                        "changed_file": "charts/grafana/values-dev.yaml",
                        "detail": "triggered",
                    }
                ],
            }
        ],
        "chart_tests": [
            {
                "chart": "grafana",
                "profile": "minimal",
                "reasons": [
                    {
                        "code": "chart-change",
                        "changed_file": "charts/grafana/values-dev.yaml",
                        "detail": "grafana changed",
                    }
                ],
            },
            {"chart": "loki", "profile": "full", "reasons": []},
        ],
        "publish": ["grafana"],
        "spec_errors": [],
        "warnings": [],
    }
