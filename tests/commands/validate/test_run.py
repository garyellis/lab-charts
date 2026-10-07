"""`run()`: one request in, one row per chart and environment out."""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from chart_manager.commands import validate
from chart_manager.commands.validate.models import CheckName
from chart_manager.commands.validate.run import run
from chart_manager.plumbing.errors import MissingToolError, SpecError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.shared.workspace import RepositoryWorkspace
from tests.conftest import (
    ONE_DEPENDENCY_LOCK,
    FakeCommandRunner,
    crd_manifest,
    git_runner,
    materialize_dependency,
    workspace_for,
    write_validation_chart,
)

RENDER: frozenset[CheckName] = frozenset({"render"})
CONFIG_MAP = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n"
APP_WITH_ONE_DEPENDENCY = (
    "apiVersion: v2\nname: app\nversion: 0.1.0\ndependencies:\n"
    "  - name: foo\n    version: 1.0.0\n    repository: https://example.test/charts\n"
)


def _run(
    request: validate.ValidateRequest, *, workspace: RepositoryWorkspace, **kw: Any
) -> validate.ValidateOutcome:
    """`run()` with the schema cache under the workspace root, where `schema_cache` puts it."""
    cache = workspace.root / "schema-cache"
    return run(request, workspace=workspace, schema_cache_root=cache, **kw)


def renders(manifest: str):
    """Match `helm template` and write `manifest` into its --output-dir, as helm would."""

    def match(argv: tuple[str, ...]) -> bool:
        if argv[1:2] != ("template",):
            return False
        out = Path(argv[argv.index("--output-dir") + 1]) / argv[2] / "templates"
        out.mkdir(parents=True, exist_ok=True)
        (out / "manifest.yaml").write_text(manifest)
        return True

    return match


def test_one_chart_in_one_environment_renders_into_one_passed_row(tmp_path: Path) -> None:
    write_validation_chart(tmp_path, "demo")
    runner = FakeCommandRunner()

    outcome = _run(
        validate.ValidateRequest(
            out=tmp_path / "out", charts=("demo",), envs=("dev",), checks=RENDER
        ),
        workspace=workspace_for(tmp_path),
        runner=runner,
    )

    assert [(row.chart, row.env, row.checks["render"].status) for row in outcome.rows] == [
        ("demo", "dev", "passed")
    ]
    (template,) = [call for call in runner.calls if call[1] == "template"]
    assert template[2:4] == ("demo", str(tmp_path / "charts" / "demo"))
    assert ("--namespace", "lab-dev") in pairwise(template)


def test_a_helm_template_failure_fails_the_row_with_helms_error(tmp_path: Path) -> None:
    write_validation_chart(tmp_path, "demo")
    runner = FakeCommandRunner().respond(("helm", "template"), returncode=1, stderr="bad values")

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("demo",), checks=RENDER),
        workspace=workspace_for(tmp_path),
        runner=runner,
    )

    (row,) = outcome.rows
    assert row.checks["render"].status == "failed"
    assert "bad values" in row.checks["render"].detail


def test_an_unknown_environment_in_the_request_raises_before_any_work(tmp_path: Path) -> None:
    write_validation_chart(tmp_path, "demo")
    runner = FakeCommandRunner()

    with pytest.raises(SpecError, match="prod"):
        _run(
            validate.ValidateRequest(
                out=tmp_path / "out", charts=("demo",), envs=("prod",), checks=RENDER
            ),
            workspace=workspace_for(tmp_path),
            runner=runner,
        )
    assert runner.calls == []


def test_rendering_into_a_reused_out_dir_drops_the_previous_manifests(tmp_path: Path) -> None:
    write_validation_chart(tmp_path, "demo")
    stale = tmp_path / "out" / "demo" / "dev" / "stale.yaml"
    stale.parent.mkdir(parents=True)
    stale.write_text("kind: ConfigMap\n")

    _run(
        validate.ValidateRequest(charts=("demo",), out=tmp_path / "out", checks=RENDER),
        workspace=workspace_for(tmp_path),
        runner=FakeCommandRunner(),
    )

    assert not stale.exists()


def kubeconform_report(status: str, msg: str = "") -> str:
    resource = {"filename": "manifest.yaml", "kind": "ConfigMap", "name": "demo"}
    return json.dumps({"resources": [{**resource, "status": status, "msg": msg}]})


@pytest.mark.parametrize(
    ("returncode", "stdout", "status", "detail"),
    [
        (0, kubeconform_report("statusValid"), "passed", ""),
        (1, kubeconform_report("statusInvalid", "missing properties: data"), "failed", "data"),
        (2, "panic: schema cache", "error", ""),
        (
            1,
            kubeconform_report("statusError", "could not find schema for Widget"),
            "error",
            "schemas sync",
        ),
    ],
    ids=["valid", "invalid", "kubeconform-broke", "schema-missing"],
)
def test_the_schema_check_reports_kubeconforms_verdict_on_the_rendered_manifests(
    tmp_path: Path,
    schema_workspace: RepositoryWorkspace,
    returncode: int,
    stdout: str,
    status: str,
    detail: str,
) -> None:
    write_validation_chart(tmp_path, "demo")
    runner = (
        git_runner()
        .respond(renders(CONFIG_MAP))
        .respond(("kubeconform",), returncode=returncode, stdout=stdout)
    )

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("demo",)),
        workspace=schema_workspace,
        runner=runner,
    )

    (row,) = outcome.rows
    assert row.checks["schema"].status == status
    assert detail in row.checks["schema"].detail
    (kubeconform,) = [call for call in runner.calls if call[0] == "kubeconform"]
    validation = schema_workspace.spec.validation
    assert validation is not None
    assert ("-kubernetes-version", validation.kubernetes_version) in pairwise(kubeconform)


@pytest.mark.parametrize(
    ("validation", "helm"),
    [
        ({"validators": {"kubeconform": False}}, {"matcher": renders(CONFIG_MAP)}),
        ({}, {"matcher": ("helm", "template"), "returncode": 1}),
        ({}, {"matcher": ("helm", "template")}),
    ],
    ids=["kubeconform-disabled", "render-failed", "no-manifests"],
)
def test_the_schema_check_is_skipped_without_running_kubeconform(
    tmp_path: Path,
    schema_workspace: RepositoryWorkspace,
    validation: dict[str, Any],
    helm: dict[str, Any],
) -> None:
    write_validation_chart(tmp_path, "demo", **validation)
    runner = git_runner().respond(**helm)

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("demo",)),
        workspace=schema_workspace,
        runner=runner,
    )

    (row,) = outcome.rows
    assert row.checks["schema"].status == "skipped"
    assert not [call for call in runner.calls if call[0] == "kubeconform"]


def test_the_charts_schema_locations_and_ignored_kinds_reach_kubeconform(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    write_validation_chart(
        tmp_path,
        "demo",
        schemaLocations=["schemas/{{.ResourceKind}}.json"],
        ignoreMissingSchemas=["Gadget"],
    )
    (tmp_path / "schemas").mkdir()
    gadget = "apiVersion: example.io/v1\nkind: Gadget\nmetadata:\n  name: demo\n"
    runner = (
        git_runner()
        .respond(renders(gadget))
        .respond(("kubeconform",), stdout=json.dumps({"resources": []}))
    )

    _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("demo",)),
        workspace=schema_workspace,
        runner=runner,
    )

    (kubeconform,) = [call for call in runner.calls if call[0] == "kubeconform"]
    flags = list(pairwise(kubeconform))
    assert ("-schema-location", f"{tmp_path}/schemas/{{{{.ResourceKind}}}}.json") in flags
    assert "example.io/v1/Gadget" in dict(flags)["-skip"].split(",")


def test_a_chart_that_disables_kubeconform_needs_no_schema_lock(tmp_path: Path) -> None:
    write_validation_chart(tmp_path, "demo", validators={"kubeconform": False})

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("demo",)),
        workspace=workspace_for(tmp_path),
        runner=FakeCommandRunner().respond(renders(CONFIG_MAP)),
    )

    assert outcome.rows[0].checks["schema"].status == "skipped"


def test_a_schema_location_whose_directory_is_missing_is_a_spec_error(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    write_validation_chart(tmp_path, "demo", schemaLocations=["schemas/{{.ResourceKind}}.json"])
    runner = git_runner().respond(renders(CONFIG_MAP))

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("demo",)),
        workspace=schema_workspace,
        runner=runner,
    )

    assert (outcome.rows, len(outcome.spec_errors)) == ((), 1)
    assert "schemas" in outcome.spec_errors[0]


def kyverno_report(result: str, message: str = "") -> str:
    resource = {"kind": "ConfigMap", "name": "demo"}
    entry = {"policy": "require-labels", "rule": "check", "result": result, "message": message}
    return json.dumps({"results": [{**entry, "resources": [resource]}]})


@pytest.mark.parametrize(
    ("returncode", "stdout", "status", "detail"),
    [
        (0, kyverno_report("pass"), "passed", ""),
        (1, kyverno_report("fail", "label team is required"), "failed", "team"),
        (0, kyverno_report("warn", "label owner is advised"), "passed", "owner"),
        (2, "panic: policy cache", "error", ""),
    ],
    ids=["pass", "fail", "warn", "kyverno-broke"],
)
def test_the_policy_check_reports_kyvernos_verdict_on_the_rendered_manifests(
    tmp_path: Path, returncode: int, stdout: str, status: str, detail: str
) -> None:
    write_validation_chart(tmp_path, "demo")
    (tmp_path / "policies").mkdir()
    runner = (
        FakeCommandRunner()
        .respond(renders(CONFIG_MAP))
        .respond(("kyverno", "apply"), returncode=returncode, stdout=stdout)
    )

    outcome = _run(
        validate.ValidateRequest(
            out=tmp_path / "out", charts=("demo",), checks=frozenset({"render", "policy"})
        ),
        workspace=workspace_for(tmp_path),
        runner=runner,
    )

    (row,) = outcome.rows
    assert row.checks["policy"].status == status
    assert detail in row.checks["policy"].detail
    (kyverno,) = [call for call in runner.calls if call[0] == "kyverno"]
    assert str(tmp_path / "policies") in kyverno


@pytest.mark.parametrize(
    ("validation", "policies_dir"),
    [({"validators": {"policy": False}}, True), ({}, False)],
    ids=["policy-disabled", "no-policies"],
)
def test_the_policy_check_is_skipped_without_running_kyverno(
    tmp_path: Path, validation: dict[str, Any], policies_dir: bool
) -> None:
    write_validation_chart(tmp_path, "demo", **validation)
    if policies_dir:
        (tmp_path / "policies").mkdir()
    runner = FakeCommandRunner().respond(renders(CONFIG_MAP))

    outcome = _run(
        validate.ValidateRequest(
            out=tmp_path / "out", charts=("demo",), checks=frozenset({"render", "policy"})
        ),
        workspace=workspace_for(tmp_path),
        runner=runner,
    )

    assert outcome.rows[0].checks["policy"].status == "skipped"
    assert not [call for call in runner.calls if call[0] == "kyverno"]


def test_the_charts_extra_policies_reach_kyverno_and_must_exist(tmp_path: Path) -> None:
    chart = write_validation_chart(tmp_path, "demo", policies={"extra": ["extra-policies"]})
    runner = FakeCommandRunner().respond(renders(CONFIG_MAP))
    request = validate.ValidateRequest(
        out=tmp_path / "out", charts=("demo",), checks=frozenset({"render", "policy"})
    )

    missing = _run(request, workspace=workspace_for(tmp_path), runner=runner)
    assert "extra-policies" in missing.spec_errors[0]
    (chart / "extra-policies").mkdir()
    _run(request, workspace=workspace_for(tmp_path), runner=runner)

    (kyverno,) = [call for call in runner.calls if call[0] == "kyverno"]
    assert str(chart / "extra-policies") in kyverno


def test_a_failed_schema_check_skips_the_policy_check(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    write_validation_chart(tmp_path, "demo")
    (tmp_path / "policies").mkdir()
    runner = (
        git_runner()
        .respond(renders(CONFIG_MAP))
        .respond(("kubeconform",), returncode=1, stdout=kubeconform_report("statusInvalid"))
    )

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("demo",)),
        workspace=schema_workspace,
        runner=runner,
    )

    assert outcome.rows[0].checks["policy"] == validate.CheckResult("skipped", "schema failed")
    assert not [call for call in runner.calls if call[0] == "kyverno"]


def test_a_chart_whose_render_fails_skips_its_checks_while_the_others_run(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    for name in ("one", "two", "three"):
        write_validation_chart(tmp_path, name)
    (tmp_path / "policies").mkdir()
    runner = (
        git_runner()
        .respond(lambda argv: argv[1:3] == ("template", "one"), returncode=1, stderr="bad")
        .respond(renders(CONFIG_MAP))
        .respond(("kubeconform",), stdout=kubeconform_report("statusValid"))
        .respond(("kyverno", "apply"), stdout=kyverno_report("pass"))
    )

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("one", "two", "three")),
        workspace=schema_workspace,
        runner=runner,
    )

    assert {row.chart: {c: r.status for c, r in row.checks.items()} for row in outcome.rows} == {
        "one": {"render": "failed", "schema": "skipped", "policy": "skipped"},
        "two": {"render": "passed", "schema": "passed", "policy": "passed"},
        "three": {"render": "passed", "schema": "passed", "policy": "passed"},
    }


def test_a_charts_config_error_is_collected_while_the_other_charts_run(tmp_path: Path) -> None:
    write_validation_chart(tmp_path, "broken", environments={"dev": {"values": ["missing.yaml"]}})
    write_validation_chart(tmp_path, "fine")

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("broken", "fine"), checks=RENDER),
        workspace=workspace_for(tmp_path),
        runner=FakeCommandRunner(),
    )

    assert [row.chart for row in outcome.rows] == ["fine"]
    (error,) = outcome.spec_errors
    assert "broken" in error and "missing.yaml" in error


def test_without_named_charts_the_changed_files_select_the_rows(tmp_path: Path) -> None:
    write_validation_chart(
        tmp_path,
        "demo",
        environments={"dev": {"values": ["values.yaml"]}, "ci": {"values": ["values.yaml"]}},
    )
    broken = write_validation_chart(tmp_path, "broken")
    (broken / "Chart.yaml").write_text("name: other\n")

    outcome = _run(
        validate.ValidateRequest(
            out=tmp_path / "out", charts=(), changes=("charts/demo/values-ci.yaml",), checks=RENDER
        ),
        workspace=workspace_for(tmp_path),
        runner=FakeCommandRunner(),
    )

    assert [(row.chart, row.env) for row in outcome.rows] == [("demo", "ci")]
    assert [error.split(":")[0] for error in outcome.spec_errors] == ["broken"]


def test_named_charts_narrow_the_changed_rows_and_warnings_reach_the_outcome(
    tmp_path: Path,
) -> None:
    two_envs = {"dev": {"values": ["values.yaml"]}, "ci": {"values": ["values.yaml"]}}
    write_validation_chart(tmp_path, "demo", environments=two_envs)
    write_validation_chart(tmp_path, "other", environments=two_envs)
    changes = ("charts/demo/values-ci.yaml", "charts/other/values-ci.yaml", "charts/demo/notes.txt")

    outcome = _run(
        validate.ValidateRequest(
            out=tmp_path / "out", charts=("demo",), changes=changes, checks=RENDER
        ),
        workspace=workspace_for(tmp_path),
        runner=FakeCommandRunner(),
    )

    assert [(row.chart, row.env) for row in outcome.rows] == [("demo", "ci")]
    assert any("charts/demo/notes.txt" in warning for warning in outcome.warnings)


def test_an_unknown_environment_raises_when_the_rows_come_from_changes(tmp_path: Path) -> None:
    write_validation_chart(tmp_path, "demo")

    with pytest.raises(SpecError, match="prod"):
        _run(
            validate.ValidateRequest(
                out=tmp_path / "out", charts=(), changes=(), envs=("prod",), checks=RENDER
            ),
            workspace=workspace_for(tmp_path),
            runner=FakeCommandRunner(),
        )


def test_a_malformed_named_chart_is_collected_while_the_other_named_chart_runs(
    tmp_path: Path,
) -> None:
    write_validation_chart(tmp_path, "fine")
    broken = write_validation_chart(tmp_path, "broken")
    (broken / "chart-lifecycle.yaml").write_text("kind: nonsense\n")

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("broken", "fine"), checks=RENDER),
        workspace=workspace_for(tmp_path),
        runner=FakeCommandRunner(),
    )

    assert [row.chart for row in outcome.rows] == ["fine"]
    assert [error.split(":")[0] for error in outcome.spec_errors] == ["broken"]


def test_a_charts_crds_become_the_first_schema_location(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    provider = write_validation_chart(tmp_path, "provider")
    (provider / "templates").mkdir()
    (provider / "templates" / "crd.yaml").write_text(crd_manifest())
    runner = (
        git_runner()
        .respond(renders(crd_manifest()))
        .respond(("kubeconform",), stdout=kubeconform_report("statusValid"))
    )

    _run(
        validate.ValidateRequest(
            out=tmp_path / "out", charts=("provider",), checks=frozenset({"render", "schema"})
        ),
        workspace=schema_workspace,
        runner=runner,
    )

    (kubeconform,) = [call for call in runner.calls if call[0] == "kubeconform"]
    first = next(value for flag, value in pairwise(kubeconform) if flag == "-schema-location")
    assert "/derived/schemas/" in first
    assert ("helm", "template", "provider") in [
        call[:3] for call in runner.calls if "--include-crds" in call
    ]


def test_another_charts_stale_dependencies_are_updated_before_crd_providers_are_chosen(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    write_validation_chart(tmp_path, "demo")
    app = write_validation_chart(tmp_path, "app")
    (app / "Chart.yaml").write_text(APP_WITH_ONE_DEPENDENCY)

    def updates(argv: tuple[str, ...]) -> bool:
        if argv[1:3] != ("dependency", "update"):
            return False
        chart = Path(argv[3])
        (chart / "Chart.lock").write_text(ONE_DEPENDENCY_LOCK)
        (chart / "charts").mkdir(exist_ok=True)
        materialize_dependency(chart)
        return True

    runner = (
        git_runner()
        .respond(updates)
        .respond(renders(CONFIG_MAP))
        .respond(("kubeconform",), stdout=kubeconform_report("statusValid"))
    )

    _run(
        validate.ValidateRequest(
            out=tmp_path / "out", charts=("demo",), checks=frozenset({"render", "schema"})
        ),
        workspace=schema_workspace,
        runner=runner,
    )

    assert ("helm", "dependency", "update", str(app)) in runner.calls
    assert not [call for call in runner.calls if "--include-crds" in call]


def test_parallel_rows_of_one_chart_update_its_stale_dependencies_once(tmp_path: Path) -> None:
    envs = ("dev", "qa", "stage", "prod", "lab")
    app = write_validation_chart(
        tmp_path, "app", environments={env: {"values": ["values.yaml"]} for env in envs}
    )
    (app / "Chart.yaml").write_text(APP_WITH_ONE_DEPENDENCY)
    runner = FakeCommandRunner()

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("app",), checks=RENDER, workers=5),
        workspace=workspace_for(tmp_path),
        runner=runner,
    )

    assert [row.checks["render"].status for row in outcome.rows] == ["passed"] * 5
    assert [
        (record.args, record.timeout)
        for record in runner.records
        if record.args[1:3] == ("dependency", "update")
    ] == [(("helm", "dependency", "update", str(app)), 600.0)]


def test_a_failed_dependency_update_is_a_tool_error_not_a_chart_failure(tmp_path: Path) -> None:
    app = write_validation_chart(tmp_path, "app")
    (app / "Chart.yaml").write_text(APP_WITH_ONE_DEPENDENCY)
    runner = FakeCommandRunner().respond(
        ("helm", "dependency", "update"), returncode=1, stderr="registry unreachable"
    )

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("app",), checks=RENDER),
        workspace=workspace_for(tmp_path),
        runner=runner,
    )

    assert outcome.rows[0].checks["render"].status == "error"
    assert outcome.outcome() is Outcome.TOOL


def test_helm_killed_mid_render_is_an_error_not_a_chart_failure(tmp_path: Path) -> None:
    write_validation_chart(tmp_path, "demo")
    runner = FakeCommandRunner().respond(("helm", "template"), returncode=-9)

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("demo",), checks=RENDER),
        workspace=workspace_for(tmp_path),
        runner=runner,
    )

    assert outcome.rows[0].checks["render"].status == "error"


def test_a_missing_helm_binary_stops_the_run(tmp_path: Path) -> None:
    write_validation_chart(tmp_path, "demo")

    with pytest.raises(MissingToolError):
        _run(
            validate.ValidateRequest(out=tmp_path / "out", charts=("demo",), checks=RENDER),
            workspace=workspace_for(tmp_path),
            runner=FakeCommandRunner().respond(
                lambda argv: argv[0] == "helm",
                raises=MissingToolError("required tool not found on PATH: helm"),
            ),
        )


@pytest.mark.parametrize(
    ("statuses", "spec_errors", "expected"),
    [
        (["passed", "skipped"], (), Outcome.SUCCESS),
        (["passed", "failed"], (), Outcome.FAILED),
        (["failed", "error"], (), Outcome.TOOL),
        (["error"], ("broken: bad",), Outcome.SPEC),
    ],
)
def test_the_outcome_folds_rows_and_spec_errors_into_one_exit_reason(
    statuses: list[str], spec_errors: tuple[str, ...], expected: Outcome
) -> None:
    checks = {
        name: validate.CheckResult(status)
        for name, status in zip(("render", "schema"), statuses, strict=False)
    }
    row = validate.Row("demo", "dev", "demo", "lab-dev", checks)

    assert validate.ValidateOutcome(rows=(row,), spec_errors=spec_errors).outcome() is expected


class RecordingProgress:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.events: list[tuple[str, str, str, bool]] = []
        self.stopped = False

    def start(self, rows):  # type: ignore[no-untyped-def]
        self.started = [(row.chart, row.env) for row in rows]

    def on_event(self, row, check, status, elapsed_s=None):  # type: ignore[no-untyped-def]
        self.events.append((row.chart, check, status, elapsed_s is not None))

    def stop(self) -> None:
        self.stopped = True


def test_progress_hears_each_check_start_and_finish_with_its_time(tmp_path: Path) -> None:
    write_validation_chart(tmp_path, "demo")
    progress = RecordingProgress()

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("demo",), checks=RENDER),
        workspace=workspace_for(tmp_path),
        runner=FakeCommandRunner(),
        progress=progress,
    )

    assert progress.started == [("demo", "dev")]
    assert progress.events == [
        ("demo", "render", "running", False),
        ("demo", "render", "passed", True),
    ]
    assert progress.stopped
    assert outcome.rows[0].checks["render"].elapsed_seconds is not None


def test_rows_checked_in_parallel_come_back_in_selection_order(tmp_path: Path) -> None:
    names = ("a", "b", "c", "d")
    for name in names:
        write_validation_chart(tmp_path, name)

    outcome = _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=names, checks=RENDER, workers=4),
        workspace=workspace_for(tmp_path),
        runner=FakeCommandRunner(),
    )

    assert [row.chart for row in outcome.rows] == list(names)


def test_fail_fast_skips_the_rows_after_the_first_failure(tmp_path: Path) -> None:
    write_validation_chart(tmp_path, "one")
    write_validation_chart(tmp_path, "two")
    runner = FakeCommandRunner().respond(("helm", "template", "one"), returncode=1)

    outcome = _run(
        validate.ValidateRequest(
            out=tmp_path / "out", charts=("one", "two"), checks=RENDER, fail_fast=True
        ),
        workspace=workspace_for(tmp_path),
        runner=runner,
    )

    assert [row.checks["render"].status for row in outcome.rows] == ["failed", "skipped"]
    assert not [call for call in runner.calls if call[1:3] == ("template", "two")]



def test_the_schema_stores_git_calls_go_through_the_given_runner(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    write_validation_chart(tmp_path, "demo")
    runner = git_runner().respond(renders(CONFIG_MAP)).respond(("kubeconform",))

    _run(
        validate.ValidateRequest(out=tmp_path / "out", charts=("demo",)),
        workspace=schema_workspace,
        runner=runner,
    )

    assert [call for call in runner.calls if call[0] == "git"]

def test_the_tool_timeout_and_verbose_reach_every_tool_call(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    write_validation_chart(tmp_path, "demo")
    (tmp_path / "policies").mkdir()
    runner = (
        git_runner()
        .respond(renders(CONFIG_MAP))
        .respond(("kubeconform",), stdout=kubeconform_report("statusValid"))
        .respond(("kyverno", "apply"), stdout=kyverno_report("pass"))
    )

    _run(
        validate.ValidateRequest(
            out=tmp_path / "out", charts=("demo",), tool_timeout=30.0, verbose=True
        ),
        workspace=schema_workspace,
        runner=runner,
    )

    tools = {record.args[0]: record for record in runner.records}
    assert {tool: tools[tool].timeout for tool in ("helm", "kubeconform", "kyverno")} == {
        "helm": 30.0,
        "kubeconform": 30.0,
        "kyverno": 30.0,
    }
    assert tools["helm"].capture is False


def test_a_symlinked_row_directory_is_refused_rather_than_emptied(tmp_path: Path) -> None:
    write_validation_chart(tmp_path, "demo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.yaml").write_text("kind: ConfigMap\n")
    (tmp_path / "out" / "demo").mkdir(parents=True)
    (tmp_path / "out" / "demo" / "dev").symlink_to(elsewhere, target_is_directory=True)

    outcome = _run(
        validate.ValidateRequest(charts=("demo",), out=tmp_path / "out", checks=RENDER),
        workspace=workspace_for(tmp_path),
        runner=FakeCommandRunner(),
    )

    assert "symlink" in outcome.spec_errors[0]
    assert (elsewhere / "keep.yaml").exists()


def test_the_outcome_records_what_shaped_the_selection(tmp_path: Path) -> None:
    write_validation_chart(
        tmp_path,
        "demo",
        environments={"dev": {"values": ["values.yaml"]}, "ci": {"values": ["values.yaml"]}},
        triggerIgnores=["docs/**"],
    )
    (tmp_path / "charts" / "plain").mkdir()
    (tmp_path / "charts" / "plain" / "Chart.yaml").write_text(
        "apiVersion: v2\nname: plain\nversion: 0.1.0\n"
    )
    changes = ("charts/demo/templates/cm.yaml", "charts/demo/docs/a.md", "charts/demo/notes.txt")

    outcome = _run(
        validate.ValidateRequest(
            charts=(), out=tmp_path / "out", envs=("ci",), changes=changes, checks=RENDER
        ),
        workspace=workspace_for(tmp_path),
        runner=FakeCommandRunner(),
    )

    assert outcome.diagnostics == validate.Diagnostics(
        requested_envs=("ci",),
        ignored_changes=("charts/demo/docs/a.md",),
        unmatched_changes=("charts/demo/notes.txt",),
        rows_filtered_out=1,
        charts_unvalidated=1,
    )
