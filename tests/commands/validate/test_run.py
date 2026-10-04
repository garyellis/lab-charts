"""`validate.run()`: one request in, one row per chart and environment out."""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from chart_manager.commands import validate
from chart_manager.commands.validate.models import CheckName
from chart_manager.commands.validate.schemas.lock import write_schema_lock_atomic
from chart_manager.commands.validate.schemas.store import (
    KubeconformSchemaStore,
    default_schema_cache_root,
)
from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.yaml_files import dump_yaml
from chart_manager.shared.workspace import SCHEMA_LOCK_FILE, RepositoryWorkspace
from tests import schema_fixtures
from tests.conftest import FakeCommandRunner, workspace_for

RENDER: frozenset[CheckName] = frozenset({"render"})
CONFIG_MAP = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n"


def write_chart(root: Path, name: str, **validation: Any) -> Path:
    """Write a chart whose `spec.validation` is `validation` over a `dev` default."""
    chart = root / "charts" / name
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(dump_yaml({"apiVersion": "v2", "name": name, "version": "0.1.0"}))
    (chart / "values.yaml").write_text("")
    spec = {
        "releaseName": name,
        "namespaceTemplate": "lab-${env}",
        "environments": {"dev": {"values": ["values.yaml"]}},
        **validation,
    }
    (chart / "chart-lifecycle.yaml").write_text(
        dump_yaml(
            {
                "apiVersion": "chartmanager.io/v1alpha1",
                "kind": "ChartLifecycle",
                "metadata": {"name": name},
                "spec": {"validation": spec},
            }
        )
    )
    return chart


@pytest.fixture
def schema_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RepositoryWorkspace:
    """A workspace whose locked schema generation is synced into a tmp cache."""
    lock, _, snapshots = schema_fixtures.schema_store(tmp_path / "upstream")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    KubeconformSchemaStore(cache_root=default_schema_cache_root(), snapshots=snapshots).sync(lock)
    write_schema_lock_atomic(tmp_path / SCHEMA_LOCK_FILE, lock)
    return schema_fixtures.workspace(tmp_path)


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
    write_chart(tmp_path, "demo")
    runner = FakeCommandRunner()

    outcome = validate.run(
        validate.ValidateRequest(charts=("demo",), envs=("dev",), checks=RENDER),
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
    write_chart(tmp_path, "demo")
    runner = FakeCommandRunner().respond(("helm", "template"), returncode=1, stderr="bad values")

    outcome = validate.run(
        validate.ValidateRequest(charts=("demo",), checks=RENDER),
        workspace=workspace_for(tmp_path),
        runner=runner,
    )

    (row,) = outcome.rows
    assert row.checks["render"].status == "failed"
    assert "bad values" in row.checks["render"].detail


@pytest.mark.parametrize(
    ("envs", "values"),
    [(("prod",), ["values.yaml"]), (("dev",), ["values.yaml", "missing.yaml"])],
    ids=["unknown-environment", "missing-values-file"],
)
def test_a_request_the_chart_cannot_satisfy_is_a_spec_error(
    tmp_path: Path, envs: tuple[str, ...], values: list[str]
) -> None:
    write_chart(tmp_path, "demo", environments={"dev": {"values": values}})
    runner = FakeCommandRunner()

    with pytest.raises(SpecError):
        validate.run(
            validate.ValidateRequest(charts=("demo",), envs=envs, checks=RENDER),
            workspace=workspace_for(tmp_path),
            runner=runner,
        )
    assert runner.calls == []


def test_rendering_into_a_reused_out_dir_drops_the_previous_manifests(tmp_path: Path) -> None:
    write_chart(tmp_path, "demo")
    stale = tmp_path / "out" / "demo" / "dev" / "stale.yaml"
    stale.parent.mkdir(parents=True)
    stale.write_text("kind: ConfigMap\n")

    validate.run(
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
        (2, "panic: schema cache", "failed", ""),
    ],
    ids=["valid", "invalid", "kubeconform-broke"],
)
def test_the_schema_check_reports_kubeconforms_verdict_on_the_rendered_manifests(
    tmp_path: Path,
    schema_workspace: RepositoryWorkspace,
    returncode: int,
    stdout: str,
    status: str,
    detail: str,
) -> None:
    write_chart(tmp_path, "demo")
    runner = (
        FakeCommandRunner()
        .respond(renders(CONFIG_MAP))
        .respond(("kubeconform",), returncode=returncode, stdout=stdout)
    )

    outcome = validate.run(
        validate.ValidateRequest(charts=("demo",)), workspace=schema_workspace, runner=runner
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
    write_chart(tmp_path, "demo", **validation)
    runner = FakeCommandRunner().respond(**helm)

    outcome = validate.run(
        validate.ValidateRequest(charts=("demo",)), workspace=schema_workspace, runner=runner
    )

    (row,) = outcome.rows
    assert row.checks["schema"].status == "skipped"
    assert not [call for call in runner.calls if call[0] == "kubeconform"]


def test_the_charts_schema_locations_and_ignored_kinds_reach_kubeconform(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    write_chart(
        tmp_path,
        "demo",
        schemaLocations=["schemas/{{.ResourceKind}}.json"],
        ignoreMissingSchemas=["Gadget"],
    )
    (tmp_path / "schemas").mkdir()
    gadget = "apiVersion: example.io/v1\nkind: Gadget\nmetadata:\n  name: demo\n"
    runner = (
        FakeCommandRunner()
        .respond(renders(gadget))
        .respond(("kubeconform",), stdout=json.dumps({"resources": []}))
    )

    validate.run(validate.ValidateRequest(charts=("demo",)), workspace=schema_workspace, runner=runner)

    (kubeconform,) = [call for call in runner.calls if call[0] == "kubeconform"]
    flags = list(pairwise(kubeconform))
    assert ("-schema-location", f"{tmp_path}/schemas/{{{{.ResourceKind}}}}.json") in flags
    assert "example.io/v1/Gadget" in dict(flags)["-skip"].split(",")


def test_a_chart_that_disables_kubeconform_needs_no_schema_lock(tmp_path: Path) -> None:
    write_chart(tmp_path, "demo", validators={"kubeconform": False})

    outcome = validate.run(
        validate.ValidateRequest(charts=("demo",)),
        workspace=workspace_for(tmp_path),
        runner=FakeCommandRunner().respond(renders(CONFIG_MAP)),
    )

    assert outcome.rows[0].checks["schema"].status == "skipped"


def test_a_schema_location_whose_directory_is_missing_is_a_spec_error(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    write_chart(tmp_path, "demo", schemaLocations=["schemas/{{.ResourceKind}}.json"])
    runner = FakeCommandRunner().respond(renders(CONFIG_MAP))

    with pytest.raises(SpecError, match="schemas"):
        validate.run(
            validate.ValidateRequest(charts=("demo",)), workspace=schema_workspace, runner=runner
        )


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
        (2, "panic: policy cache", "failed", ""),
    ],
    ids=["pass", "fail", "warn", "kyverno-broke"],
)
def test_the_policy_check_reports_kyvernos_verdict_on_the_rendered_manifests(
    tmp_path: Path, returncode: int, stdout: str, status: str, detail: str
) -> None:
    write_chart(tmp_path, "demo")
    (tmp_path / "policies").mkdir()
    runner = (
        FakeCommandRunner()
        .respond(renders(CONFIG_MAP))
        .respond(("kyverno", "apply"), returncode=returncode, stdout=stdout)
    )

    outcome = validate.run(
        validate.ValidateRequest(charts=("demo",), checks=frozenset({"render", "policy"})),
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
    write_chart(tmp_path, "demo", **validation)
    if policies_dir:
        (tmp_path / "policies").mkdir()
    runner = FakeCommandRunner().respond(renders(CONFIG_MAP))

    outcome = validate.run(
        validate.ValidateRequest(charts=("demo",), checks=frozenset({"render", "policy"})),
        workspace=workspace_for(tmp_path),
        runner=runner,
    )

    assert outcome.rows[0].checks["policy"].status == "skipped"
    assert not [call for call in runner.calls if call[0] == "kyverno"]


def test_the_charts_extra_policies_reach_kyverno_and_must_exist(tmp_path: Path) -> None:
    chart = write_chart(tmp_path, "demo", policies={"extra": ["extra-policies"]})
    runner = FakeCommandRunner().respond(renders(CONFIG_MAP))
    request = validate.ValidateRequest(charts=("demo",), checks=frozenset({"render", "policy"}))

    with pytest.raises(SpecError, match="extra-policies"):
        validate.run(request, workspace=workspace_for(tmp_path), runner=runner)
    (chart / "extra-policies").mkdir()
    validate.run(request, workspace=workspace_for(tmp_path), runner=runner)

    (kyverno,) = [call for call in runner.calls if call[0] == "kyverno"]
    assert str(chart / "extra-policies") in kyverno


def test_a_failed_schema_check_skips_the_policy_check(
    tmp_path: Path, schema_workspace: RepositoryWorkspace
) -> None:
    write_chart(tmp_path, "demo")
    (tmp_path / "policies").mkdir()
    runner = (
        FakeCommandRunner()
        .respond(renders(CONFIG_MAP))
        .respond(("kubeconform",), returncode=1, stdout=kubeconform_report("statusInvalid"))
    )

    outcome = validate.run(
        validate.ValidateRequest(charts=("demo",)), workspace=schema_workspace, runner=runner
    )

    assert outcome.rows[0].checks["policy"] == validate.CheckResult("skipped", "schema failed")
    assert not [call for call in runner.calls if call[0] == "kyverno"]
