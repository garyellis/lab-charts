"""Unit tests for the kubeconform integration / parser.

Fixtures under tests/fixtures/kubeconform/ are captured from real
kubeconform 0.8.0 output and pinned. If the kubeconform schema or output
format changes in a future release, regenerate via:

    kubeconform -output json -summary -strict \\
      -schema-location /path/to/local/{{.ResourceKind}}.json \\
      tests/fixtures/charts/passing-app/templates  > tests/fixtures/kubeconform/valid.json
    kubeconform -output json -summary -strict \\
      -schema-location /path/to/local/{{.ResourceKind}}.json \\
      tests/fixtures/charts/schema-violator/templates > tests/fixtures/kubeconform/invalid.json

The schema-violator fixture deliberately sets Deployment.spec.replicas
to a string ("high") — a fundamental JSON-schema type mismatch that is
stable across upstream kubernetes-json-schema versions.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.commands.validate.schemas.runtime import UNSUPPORTED_CRD_OBJECT_GVK
from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.plumbing.errors import ExternalCommandError, SpecError
from tests.conftest import FakeCommandRunner, OnPath

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "kubeconform"
LOCAL_SCHEMA_TEMPLATE = "/cache/schemas/{{.ResourceKind}}.json"


@pytest.mark.parametrize("exceptions,expected", [
    (frozenset(), []),
    (frozenset({"apiextensions.k8s.io/v1/CustomResourceDefinition"}),
     ["apiextensions.k8s.io/v1/CustomResourceDefinition"]),
])
def test_exact_exceptions_do_not_parse_manifests(tmp_path, monkeypatch, exceptions, expected):
    from chart_manager.integrations.kubeconform.runner import _uncovered_gvk_skips

    (tmp_path / "resource.yaml").write_text("apiVersion: v1\nkind: ConfigMap\n")
    def unexpected(*args, **kwargs):
        pytest.fail("no manifest parsing is needed for exact GVK exceptions")
    monkeypatch.setattr("chart_manager.integrations.kubeconform.runner.load_yaml_documents", unexpected)
    assert _uncovered_gvk_skips(tmp_path, [LOCAL_SCHEMA_TEMPLATE], exceptions) == expected



def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


def test_args_require_local_schemas_without_implicit_crd_skip(tmp_path: Path) -> None:
    runner = FakeCommandRunner(returncode=0, stdout=_load("valid.json"))
    kc = Kubeconform(runner=runner)

    kc.validate(tmp_path, schema_locations=[LOCAL_SCHEMA_TEMPLATE])

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call[0] == "kubeconform"
    assert "-output" in call and call[call.index("-output") + 1] == "json"
    assert "-summary" in call
    assert "-strict" in call
    assert "-skip" not in call
    schema_indices = [i for i, a in enumerate(call) if a == "-schema-location"]
    assert len(schema_indices) == 1
    assert call[schema_indices[0] + 1] == LOCAL_SCHEMA_TEMPLATE


@pytest.mark.parametrize(
    "locations",
    [
        [""],
        ["default"],
        ["//schemas.example.test/schema.json"],
        ["https://schemas.example.test/{{.ResourceKind}}.json"],
    ],
)
def test_rejects_missing_or_remote_schema_locations(
    tmp_path: Path,
    locations: list[str],
) -> None:
    runner = FakeCommandRunner(returncode=0, stdout=_load("valid.json"))
    kc = Kubeconform(runner=runner)

    with pytest.raises(ExternalCommandError, match="local"):
        kc.validate(tmp_path, schema_locations=locations)


def test_missing_schema_configuration_is_actionable_spec_error(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    with pytest.raises(SpecError, match=r"workspace.yaml.*schemas sync --update.*schemaLocations"):
        Kubeconform(runner=runner).validate(tmp_path, schema_locations=[])
    assert not runner.calls


@pytest.mark.parametrize("variable", ["Typo", "NormalizedKubernetesVersion"])
def test_unknown_template_variable_cannot_silently_skip_allowed_kind(
    tmp_path: Path, variable: str,
) -> None:
    (tmp_path / "resource.yaml").write_text(
        "apiVersion: example.io/v1\nkind: Widget\nmetadata: {name: demo}\n"
    )
    runner = FakeCommandRunner()
    with pytest.raises(SpecError, match="unsupported schema location expression"):
        Kubeconform(runner=runner).validate(
            tmp_path, schema_locations=[f"/schemas/{{{{.{variable}}}}}/widget.json"],
            skip_kinds=["Widget"],
        )
    assert not runner.calls

    assert runner.calls == []


def test_kube_version_and_overrides_passed_through(tmp_path: Path) -> None:
    runner = FakeCommandRunner(returncode=0, stdout=_load("valid.json"))
    kc = Kubeconform(runner=runner)
    (tmp_path / "resources.yaml").write_text(
        "apiVersion: apiextensions.k8s.io/v1\n"
        "kind: CustomResourceDefinition\nmetadata: {name: widgets.example.io}\n"
        "---\napiVersion: policy/v1\nkind: PodDisruptionBudget\n"
        "metadata: {name: demo}\n"
    )

    kc.validate(
        tmp_path,
        kubernetes_version="1.31.2",
        schema_locations=["/local/schemas"],
        skip_kinds=["CustomResourceDefinition", "PodDisruptionBudget"],
        strict=False,
        extra_args=["-cache", "/tmp/kc"],
    )

    call = runner.calls[0]
    assert "-strict" not in call
    assert call[call.index("-kubernetes-version") + 1] == "1.31.2"
    assert call[call.index("-schema-location") + 1] == "/local/schemas"
    assert call[call.index("-skip") + 1] == (
        "apiextensions.k8s.io/v1/CustomResourceDefinition,policy/v1/PodDisruptionBudget"
    )
    assert "-ignore-missing-schemas" not in call
    assert "-cache" in call and call[call.index("-cache") + 1] == "/tmp/kc"


def test_allow_missing_kind_is_not_skipped_when_exact_schema_exists(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    (rendered / "widget.yaml").write_text(
        "apiVersion: example.io/v1\nkind: Widget\nmetadata: {name: demo}\n"
    )
    schemas = tmp_path / "schemas"
    (schemas / "example.io").mkdir(parents=True)
    (schemas / "example.io/widget_v1.json").write_text("{}")
    runner = FakeCommandRunner(returncode=0, stdout=_load("valid.json"))

    Kubeconform(runner=runner).validate(
        rendered,
        schema_locations=[
            str(schemas / "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json")
        ],
        skip_kinds=["Widget"],
    )

    assert "-skip" not in runner.calls[0]


def test_crd_exception_is_exact_and_does_not_skip_same_kind_elsewhere(
    tmp_path: Path,
) -> None:
    (tmp_path / "resources.yaml").write_text(
        "apiVersion: apiextensions.k8s.io/v1\n"
        "kind: CustomResourceDefinition\nmetadata: {name: widgets.example.io}\n"
        "---\n"
        "apiVersion: example.io/v9\n"
        "kind: CustomResourceDefinition\nmetadata: {name: not-a-crd}\n"
    )
    runner = FakeCommandRunner(returncode=0, stdout=_load("valid.json"))

    Kubeconform(runner=runner).validate(
        tmp_path,
        schema_locations=[LOCAL_SCHEMA_TEMPLATE],
        skip_kinds=[UNSUPPORTED_CRD_OBJECT_GVK],
    )

    call = runner.calls[0]
    assert call[call.index("-skip") + 1] == UNSUPPORTED_CRD_OBJECT_GVK


def test_crd_exception_is_not_used_when_managed_schema_exists(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    (rendered / "crd.yaml").write_text(
        "apiVersion: apiextensions.k8s.io/v1\n"
        "kind: CustomResourceDefinition\nmetadata: {name: widgets.example.io}\n"
    )
    schemas = tmp_path / "schemas" / "apiextensions.k8s.io"
    schemas.mkdir(parents=True)
    (schemas / "customresourcedefinition_v1.json").write_text("{}")
    runner = FakeCommandRunner(returncode=0, stdout=_load("valid.json"))

    Kubeconform(runner=runner).validate(
        rendered,
        schema_locations=[
            str(
                tmp_path
                / "schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
            )
        ],
        skip_kinds=[UNSUPPORTED_CRD_OBJECT_GVK],
    )

    assert "-skip" not in runner.calls[0]


def test_valid_fixture_parses_to_zero_invalid(tmp_path: Path) -> None:
    runner = FakeCommandRunner(returncode=0, stdout=_load("valid.json"))
    kc = Kubeconform(runner=runner)

    report = kc.validate(tmp_path, schema_locations=[LOCAL_SCHEMA_TEMPLATE])

    # Non-verbose kubeconform emits an empty resources list when everything
    # passes; the summary is the source of truth.
    assert report.invalid() == ()
    assert report.has_failures() is False
    assert report.summary["valid"] == 2
    assert report.summary["invalid"] == 0


def test_invalid_fixture_populates_invalid_with_expected_finding(tmp_path: Path) -> None:
    runner = FakeCommandRunner(returncode=1, stdout=_load("invalid.json"))
    kc = Kubeconform(runner=runner)

    report = kc.validate(tmp_path, schema_locations=[LOCAL_SCHEMA_TEMPLATE])

    invalids = report.invalid()
    assert len(invalids) == 1
    finding = invalids[0]
    assert finding.kind == "Deployment"
    assert finding.name == "violator"
    assert finding.status == "invalid"
    assert finding.msg is not None
    assert "/spec/replicas" in finding.msg
    assert "got string, want null or integer" in finding.msg
    assert report.has_failures() is True


def test_tool_error_fixture_raises_external_command_error(tmp_path: Path) -> None:
    runner = FakeCommandRunner(returncode=2, stdout=_load("tool-error.json"), stderr="kubeconform: panic")
    kc = Kubeconform(runner=runner)

    with pytest.raises(ExternalCommandError) as exc:
        kc.validate(tmp_path, schema_locations=[LOCAL_SCHEMA_TEMPLATE])

    msg = str(exc.value)
    assert "kubeconform produced unparseable output" in msg
    assert "kubeconform: panic" in msg


def test_empty_resources_list_with_rc_zero_is_pass(tmp_path: Path) -> None:
    runner = FakeCommandRunner(
        returncode=0,
        stdout='{"resources": [], "summary": {"valid": 0, "invalid": 0, "errors": 0, "skipped": 0}}',
    )
    kc = Kubeconform(runner=runner)

    report = kc.validate(tmp_path, schema_locations=[LOCAL_SCHEMA_TEMPLATE])

    assert report.resources == ()
    assert report.has_failures() is False


def test_nonzero_rc_with_parseable_json_returns_report_without_raising(tmp_path: Path) -> None:
    # kubeconform exits non-zero whenever any resource is invalid, but still
    # writes a well-formed JSON report. The integration must NOT confuse this
    # with a tool crash — only unparseable output should raise.
    runner = FakeCommandRunner(returncode=1, stdout=_load("invalid.json"), stderr="")
    kc = Kubeconform(runner=runner)

    report = kc.validate(tmp_path, schema_locations=[LOCAL_SCHEMA_TEMPLATE])

    assert report.has_failures() is True
    assert len(report.invalid()) == 1


def test_unknown_status_string_maps_to_error(tmp_path: Path) -> None:
    runner = FakeCommandRunner(
        returncode=1,
        stdout=(
            '{"resources": [{"filename": "x.yaml", "kind": "Foo", "name": "y", '
            '"status": "statusUnknownFuture", "msg": "weird"}], '
            '"summary": {"valid": 0, "invalid": 0, "errors": 1, "skipped": 0}}'
        ),
    )
    kc = Kubeconform(runner=runner)

    report = kc.validate(tmp_path, schema_locations=[LOCAL_SCHEMA_TEMPLATE])

    assert report.invalid()[0].status == "error"


def test_kubeconform_owns_its_version_flag(on_path: OnPath) -> None:
    """The surface must never learn that kubeconform spells it `-v`."""
    on_path("kubeconform")
    runner = FakeCommandRunner(stdout="v0.6.7\n")

    Kubeconform(runner).preflight()

    assert ("kubeconform", "-v") in runner.calls
