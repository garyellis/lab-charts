"""Unit tests for the kubeconform integration / parser.

Fixtures under tests/fixtures/kubeconform/ are captured from real
kubeconform 0.8.0 output and pinned. If the kubeconform schema or output
format changes in a future release, regenerate via:

    kubeconform -output json -summary -strict \\
      -skip CustomResourceDefinition \\
      tests/fixtures/charts/passing-app/templates  > tests/fixtures/kubeconform/valid.json
    kubeconform -output json -summary -strict \\
      -skip CustomResourceDefinition \\
      tests/fixtures/charts/schema-violator/templates > tests/fixtures/kubeconform/invalid.json

The schema-violator fixture deliberately sets Deployment.spec.replicas
to a string ("high") — a fundamental JSON-schema type mismatch that is
stable across upstream kubernetes-json-schema versions.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.plumbing.commands import CommandResult, CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "kubeconform"


class StubRunner(CommandRunner):
    def __init__(self, *, returncode: int, stdout: str, stderr: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(args))
        return CommandResult(
            args=tuple(args),
            returncode=self._returncode,
            stdout=self._stdout,
            stderr=self._stderr,
        )


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


def test_default_args_include_strict_summary_json_and_crd_skip(tmp_path: Path) -> None:
    runner = StubRunner(returncode=0, stdout=_load("valid.json"))
    kc = Kubeconform(runner=runner)

    kc.validate(tmp_path)

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call[0] == "kubeconform"
    assert "-output" in call and call[call.index("-output") + 1] == "json"
    assert "-summary" in call
    assert "-strict" in call
    assert "-skip" in call and call[call.index("-skip") + 1] == "CustomResourceDefinition"
    # default schema locations: 'default' + datreeio CRDs catalog
    schema_indices = [i for i, a in enumerate(call) if a == "-schema-location"]
    assert len(schema_indices) == 2
    assert call[schema_indices[0] + 1] == "default"
    assert call[schema_indices[1] + 1] == Kubeconform.SCHEMA_LOCATION_CRDS


def test_kube_version_and_overrides_passed_through(tmp_path: Path) -> None:
    runner = StubRunner(returncode=0, stdout=_load("valid.json"))
    kc = Kubeconform(runner=runner)

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
    assert call[call.index("-skip") + 1] == "CustomResourceDefinition,PodDisruptionBudget"
    assert "-cache" in call and call[call.index("-cache") + 1] == "/tmp/kc"


def test_valid_fixture_parses_to_zero_invalid(tmp_path: Path) -> None:
    runner = StubRunner(returncode=0, stdout=_load("valid.json"))
    kc = Kubeconform(runner=runner)

    report = kc.validate(tmp_path)

    # Non-verbose kubeconform emits an empty resources list when everything
    # passes; the summary is the source of truth.
    assert report.invalid() == ()
    assert report.has_failures() is False
    assert report.summary["valid"] == 2
    assert report.summary["invalid"] == 0


def test_invalid_fixture_populates_invalid_with_expected_finding(tmp_path: Path) -> None:
    runner = StubRunner(returncode=1, stdout=_load("invalid.json"))
    kc = Kubeconform(runner=runner)

    report = kc.validate(tmp_path)

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
    runner = StubRunner(returncode=2, stdout=_load("tool-error.json"), stderr="kubeconform: panic")
    kc = Kubeconform(runner=runner)

    with pytest.raises(ExternalCommandError) as exc:
        kc.validate(tmp_path)

    msg = str(exc.value)
    assert "kubeconform produced unparseable output" in msg
    assert "kubeconform: panic" in msg


def test_empty_resources_list_with_rc_zero_is_pass(tmp_path: Path) -> None:
    runner = StubRunner(
        returncode=0,
        stdout='{"resources": [], "summary": {"valid": 0, "invalid": 0, "errors": 0, "skipped": 0}}',
    )
    kc = Kubeconform(runner=runner)

    report = kc.validate(tmp_path)

    assert report.resources == ()
    assert report.has_failures() is False


def test_nonzero_rc_with_parseable_json_returns_report_without_raising(tmp_path: Path) -> None:
    # kubeconform exits non-zero whenever any resource is invalid, but still
    # writes a well-formed JSON report. The integration must NOT confuse this
    # with a tool crash — only unparseable output should raise.
    runner = StubRunner(returncode=1, stdout=_load("invalid.json"), stderr="")
    kc = Kubeconform(runner=runner)

    report = kc.validate(tmp_path)

    assert report.has_failures() is True
    assert len(report.invalid()) == 1


def test_unknown_status_string_maps_to_error(tmp_path: Path) -> None:
    runner = StubRunner(
        returncode=1,
        stdout=(
            '{"resources": [{"filename": "x.yaml", "kind": "Foo", "name": "y", '
            '"status": "statusUnknownFuture", "msg": "weird"}], '
            '"summary": {"valid": 0, "invalid": 0, "errors": 1, "skipped": 0}}'
        ),
    )
    kc = Kubeconform(runner=runner)

    report = kc.validate(tmp_path)

    assert report.invalid()[0].status == "error"
