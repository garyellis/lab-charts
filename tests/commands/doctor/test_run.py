"""`doctor.run`: every check, folded into one outcome by the documented precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.commands.doctor.run import run
from chart_manager.plumbing.errors import WorkspaceNotFoundError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import CheckStatus
from chart_manager.settings import Settings
from chart_manager.shared.workspace import RepositoryWorkspace
from tests.conftest import FakeCommandRunner, OnPath

_TOOLCHAIN = (
    "helm",
    "kubeconform",
    "kyverno",
    "kubectl",
    "kind",
    "docker",
    "git",
    "gh",
    "renovate",
    "renovate-config-validator",
)


def _outside() -> RepositoryWorkspace:
    raise WorkspaceNotFoundError("no workspace.yaml found")


@pytest.fixture
def healthy(on_path: OnPath, monkeypatch: pytest.MonkeyPatch) -> FakeCommandRunner:
    """A machine where every tool runs and every token is set."""
    on_path(*_TOOLCHAIN)
    monkeypatch.setenv("RENOVATE_TOKEN", "fake")
    return FakeCommandRunner(stdout="v1.0.0\n")


def test_every_check_runs_in_report_order(healthy: FakeCommandRunner, tmp_path: Path) -> None:
    report = run(settings=Settings(root=tmp_path), runner=healthy, workspace=_outside)

    assert [check.name for check in report.checks] == [
        "helm",
        "kubeconform",
        "kyverno",
        "kubectl",
        "kube-context",
        "kind",
        "docker",
        "docker-daemon",
        "git",
        "git-repository",
        "gh",
        "gh-auth",
        "renovate",
        "renovate-config-validator",
        "renovate-token",
        "schema-policy",
        "schema-lock",
        "schema-store",
        "events-backend",
    ]
    assert report.outcome is Outcome.SUCCESS


def test_outside_a_workspace_the_schema_checks_are_skipped_with_the_reason(
    healthy: FakeCommandRunner, tmp_path: Path
) -> None:
    report = run(settings=Settings(root=tmp_path), runner=healthy, workspace=_outside)

    schemas = [check for check in report.checks if check.name.startswith("schema-")]
    assert {check.status for check in schemas} == {CheckStatus.SKIPPED}
    assert {check.detail for check in schemas} == {"no workspace.yaml found"}


@pytest.mark.parametrize(
    ("missing", "env", "expected"),
    [
        ((), {"RENOVATE_TOKEN": None}, Outcome.ENVIRONMENT),
        (("helm",), {"RENOVATE_TOKEN": None}, Outcome.MISSING_BINARY),
    ],
    ids=["environment", "missing-binary-beats-environment"],
)
def test_the_outcome_follows_the_documented_precedence(
    healthy: FakeCommandRunner,
    on_path: OnPath,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing: tuple[str, ...],
    env: dict[str, str | None],
    expected: Outcome,
) -> None:
    on_path(*(name for name in _TOOLCHAIN if name not in missing))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    report = run(settings=Settings(root=tmp_path), runner=healthy, workspace=_outside)

    assert report.outcome is expected


def test_a_broken_tool_is_a_tool_outcome(healthy: FakeCommandRunner, tmp_path: Path) -> None:
    healthy.respond(("helm", "version"), returncode=1, stderr="Error: unknown flag\n")

    report = run(settings=Settings(root=tmp_path), runner=healthy, workspace=_outside)

    assert report.outcome is Outcome.TOOL


def test_a_check_that_raises_becomes_a_failed_check_not_a_crash(
    healthy: FakeCommandRunner, tmp_path: Path
) -> None:
    """One broken adapter must not cost the operator the other answers."""
    healthy.respond(("kind", "version"), raises=RuntimeError("boom"))

    report = run(settings=Settings(root=tmp_path), runner=healthy, workspace=_outside)

    kind = next(check for check in report.checks if check.name == "kind")
    assert kind.status is CheckStatus.FAILED
    assert "RuntimeError" in kind.detail
    assert report.outcome is Outcome.ENVIRONMENT
    assert report.checks[-1].name == "events-backend", "later checks still ran"
