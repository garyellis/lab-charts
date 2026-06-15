"""Unit tests for the kyverno integration / parser.

Fixtures under tests/fixtures/kyverno/ are captured from real kyverno
1.18.1 output and pinned. Regenerate via:

    helm template passing tests/fixtures/charts/passing-app \\
      --output-dir /tmp/r
    kyverno apply policies/ --resource /tmp/r/passing-app/templates/ \\
      --policy-report --output-format json > tests/fixtures/kyverno/pass.json

    helm template viol tests/fixtures/charts/policy-violator \\
      --output-dir /tmp/r
    kyverno apply policies/ --resource /tmp/r/policy-violator/templates/ \\
      --policy-report --output-format json > tests/fixtures/kyverno/fail.json

`tool-error.json` is hand-written malformed output simulating a kyverno
crash; it never needs regeneration.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from chart_manager.integrations.kyverno import Kyverno
from chart_manager.plumbing.commands import CommandResult, CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "kyverno"


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


def _seed_manifest(dir_: Path, name: str = "deploy.yaml") -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    target = dir_ / name
    target.write_text("kind: Deployment\n")
    return target


def test_apply_args_include_policy_report_json_and_per_file_resources(tmp_path: Path) -> None:
    manifest = _seed_manifest(tmp_path)
    runner = StubRunner(returncode=0, stdout=_load("pass.json"))
    ky = Kyverno(runner=runner)

    ky.apply(tmp_path, policy_paths=[Path("/policies/a"), Path("/policies/b.yaml")])

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call[0] == "kyverno"
    assert call[1] == "apply"
    # Policies are positional args.
    assert "/policies/a" in call
    assert "/policies/b.yaml" in call
    # Each manifest is passed as its own --resource flag (kyverno's
    # --resource <dir> does not recurse, so we expand to file paths).
    resource_indices = [i for i, a in enumerate(call) if a == "--resource"]
    assert len(resource_indices) == 1
    assert call[resource_indices[0] + 1] == str(manifest)
    assert "--policy-report" in call
    assert "--output-format" in call and call[call.index("--output-format") + 1] == "json"


def test_apply_recurses_into_subdirectories(tmp_path: Path) -> None:
    # Helm template writes to <out>/<chart>/templates/*.yaml so the
    # integration must walk the tree itself.
    nested = tmp_path / "myapp" / "templates"
    a = _seed_manifest(nested, "deployment.yaml")
    b = _seed_manifest(nested, "service.yml")
    runner = StubRunner(returncode=0, stdout=_load("pass.json"))
    ky = Kyverno(runner=runner)

    ky.apply(tmp_path, policy_paths=[Path("/p")])

    call = runner.calls[0]
    resources = [call[i + 1] for i, a in enumerate(call) if a == "--resource"]
    assert set(resources) == {str(a), str(b)}


def test_apply_empty_manifests_dir_short_circuits_without_invoking_kyverno(tmp_path: Path) -> None:
    # An empty rendered tree must not shell out — kyverno without
    # --resource args would otherwise hang or error confusingly.
    runner = StubRunner(returncode=0, stdout=_load("pass.json"))
    ky = Kyverno(runner=runner)

    report = ky.apply(tmp_path, policy_paths=[Path("/p")])

    assert runner.calls == []
    assert report.results == ()
    assert report.has_failures() is False


def test_apply_empty_policy_paths_raises_value_error(tmp_path: Path) -> None:
    ky = Kyverno(runner=StubRunner(returncode=0, stdout=""))
    with pytest.raises(ValueError):
        ky.apply(tmp_path, policy_paths=[])


def test_pass_fixture_parses_to_zero_failures(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    runner = StubRunner(returncode=0, stdout=_load("pass.json"))
    ky = Kyverno(runner=runner)

    report = ky.apply(tmp_path, policy_paths=[Path("/p")])

    assert report.failures() == ()
    assert report.has_failures() is False
    # Captured pass.json from passing-app: 2 rules pass (require-non-root +
    # forbid-load-balancer), each against a single resource.
    assert report.summary.get("pass") == 2
    assert report.summary.get("fail") == 0
    # Every result entry still surfaces — the phase just sees them as "pass".
    assert len(report.results) >= 1
    assert all(r.status == "pass" for r in report.results)


def test_fail_fixture_populates_failures_with_expected_policy(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    runner = StubRunner(returncode=1, stdout=_load("fail.json"))
    ky = Kyverno(runner=runner)

    report = ky.apply(tmp_path, policy_paths=[Path("/p")])

    failures = report.failures()
    assert len(failures) == 1
    finding = failures[0]
    assert finding.policy == "require-non-root"
    assert finding.rule == "containers-must-run-as-non-root"
    assert finding.resource_kind == "Deployment"
    assert finding.status == "fail"
    assert finding.message is not None
    assert "runAsNonRoot" in finding.message
    assert report.has_failures() is True


def test_tool_error_fixture_raises_external_command_error(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    runner = StubRunner(
        returncode=2, stdout=_load("tool-error.json"), stderr="kyverno: panic"
    )
    ky = Kyverno(runner=runner)

    with pytest.raises(ExternalCommandError) as exc:
        ky.apply(tmp_path, policy_paths=[Path("/p")])

    msg = str(exc.value)
    assert "kyverno produced unparseable output" in msg
    assert "kyverno: panic" in msg


def test_empty_stdout_returns_empty_report_without_raising(tmp_path: Path) -> None:
    # kyverno exits 0 with empty output when --resource targets files with
    # no kyverno-recognized resources (e.g. NOTES.txt rendered as .yaml).
    # The phase function decides what to do; the integration must not
    # treat this as a parse failure.
    _seed_manifest(tmp_path)
    runner = StubRunner(returncode=0, stdout="")
    ky = Kyverno(runner=runner)

    report = ky.apply(tmp_path, policy_paths=[Path("/p")])

    assert report.results == ()
    assert report.summary == {}
    assert report.has_failures() is False


def test_nonzero_rc_with_parseable_json_returns_report_without_raising(tmp_path: Path) -> None:
    # kyverno exits non-zero whenever any policy fails, but still writes
    # a well-formed JSON report. The integration must NOT confuse this
    # with a tool crash — only unparseable output should raise.
    _seed_manifest(tmp_path)
    runner = StubRunner(returncode=1, stdout=_load("fail.json"), stderr="")
    ky = Kyverno(runner=runner)

    report = ky.apply(tmp_path, policy_paths=[Path("/p")])

    assert report.has_failures() is True
    assert len(report.failures()) == 1


def test_unknown_result_string_maps_to_error(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    runner = StubRunner(
        returncode=1,
        stdout=(
            '{"results": [{"policy": "p", "rule": "r", '
            '"resources": [{"kind": "Foo", "name": "y", "namespace": "ns"}], '
            '"result": "unknownFuture", "message": "weird"}], '
            '"summary": {"pass": 0, "fail": 0, "warn": 0, "error": 1, "skip": 0}}'
        ),
    )
    ky = Kyverno(runner=runner)

    report = ky.apply(tmp_path, policy_paths=[Path("/p")])

    assert report.results[0].status == "error"
    assert report.failures() == report.results


def test_argv_length_guard_raises_value_error(tmp_path: Path) -> None:
    # Seed enough manifests that the rendered argv exceeds the 512KB cap.
    # Filenames must stay under the OS NAME_MAX (~255), so use ~200-char
    # stems and lots of files (~3500). With pytest's tmp_path prefix
    # (~80 bytes) plus ~210-byte names + `--resource` overhead, ~3500
    # entries crosses the cap.
    nested = tmp_path / "many"
    nested.mkdir()
    long_stem = "x" * 200
    for i in range(3500):
        (nested / f"{long_stem}-{i}.yaml").write_text("kind: Pod\n")
    runner = StubRunner(returncode=0, stdout=_load("pass.json"))
    ky = Kyverno(runner=runner)

    with pytest.raises(ValueError) as exc:
        ky.apply(tmp_path, policy_paths=[Path("/p")])

    assert "argv exceeds" in str(exc.value)
    assert runner.calls == []


def test_extra_args_passed_through(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    runner = StubRunner(returncode=0, stdout=_load("pass.json"))
    ky = Kyverno(runner=runner)

    ky.apply(tmp_path, policy_paths=[Path("/p")], extra_args=["--cluster-wide-resources"])

    assert "--cluster-wide-resources" in runner.calls[0]
