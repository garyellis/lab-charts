"""The preflight capability: the shared probe, who owns a check, and the fold.

Every test here fakes both halves of the outside world -- `shutil.which` for
PATH and `FakeCommandRunner` for the subprocess -- so the suite says the same
thing on a laptop with the whole toolchain installed and on a CI runner with
none of it. A test that passed only because the developer happened to have
`kind` would be testing the machine, not the code.

The CLI's half of `doctor` (projections, exit codes, `--for` validation) is
in `tests/test_cli_doctor.py`.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import typer.main

from chart_manager.integrations.git import Git
from chart_manager.integrations.github import Github
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.integrations.kyverno import Kyverno
from chart_manager.integrations.renovate import Renovate
from chart_manager.plumbing.commands import CommandResult
from chart_manager.plumbing.errors import CommandTimeout
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import (
    PROBE_TIMEOUT,
    Check,
    CheckStatus,
    probe_binary,
)
from chart_manager.services.doctor import COMMAND_REQUIREMENTS, DoctorService
from chart_manager.services.events.store import preflight_event_store

from .conftest import FakeCommandRunner, _root_app

#: Where the fake PATH lookup claims everything lives.
_BIN = "/opt/fake/bin"

OnPath = Callable[..., None]


@pytest.fixture
def on_path(monkeypatch: pytest.MonkeyPatch) -> OnPath:
    """Control what `probe_binary` finds on PATH, for this test only.

    Patching `shutil.which` rather than injecting a lookup keeps the
    adapters' `preflight()` signatures at zero arguments, which is what lets
    the composition root bind them as bare bound methods.
    """

    def install(*names: str) -> None:
        present = set(names)

        def which(binary: str, *args: Any, **kwargs: Any) -> str | None:
            if binary not in present:
                return None
            # An absolute name (a mise-resolved helm) is already a path.
            return binary if binary.startswith("/") else f"{_BIN}/{binary}"

        monkeypatch.setattr(shutil, "which", which)

    return install


def _by_name(checks: Sequence[Check]) -> dict[str, Check]:
    """Index a preflight result by check name."""
    return {check.name: check for check in checks}


# --- the shared probe -------------------------------------------------------


def test_a_binary_missing_from_path_is_a_missing_binary_outcome(on_path: OnPath) -> None:
    """127 is reserved for "not installed", and this is where that starts."""
    on_path()
    runner = FakeCommandRunner(when_exhausted="raise")

    check = probe_binary(runner, "helm", name="helm", remediation="install helm")

    assert check.status is CheckStatus.FAILED
    assert check.outcome is Outcome.MISSING_BINARY
    assert check.remediation == "install helm"
    assert runner.calls == [], "an absent binary must not cost a subprocess"


def test_a_binary_that_runs_reports_its_version_and_path(on_path: OnPath) -> None:
    """"Which helm" is half the answer when two are installed."""
    on_path("helm")
    runner = FakeCommandRunner(stdout="v3.16.2+gf1234\n")

    check = probe_binary(runner, "helm", name="helm", remediation="install helm")

    assert check.status is CheckStatus.OK
    assert check.outcome is Outcome.SUCCESS
    assert "v3.16.2+gf1234" in check.detail
    assert f"{_BIN}/helm" in check.detail


def test_a_binary_that_is_present_but_broken_is_a_tool_failure(on_path: OnPath) -> None:
    """Installed-and-broken is exit 4, not 127.

    The distinction is the whole reason the PATH lookup is separate: a
    wrapper that installs the toolchain when it sees 127 must not fire for a
    helm that is right there and segfaulting.
    """
    on_path("helm")
    runner = FakeCommandRunner(returncode=1, stderr="Error: unknown flag: --short\n")

    check = probe_binary(runner, "helm", name="helm", remediation="install helm")

    assert check.status is CheckStatus.FAILED
    assert check.outcome is Outcome.TOOL
    assert "unknown flag" in check.detail


def test_a_probe_that_times_out_is_reported_not_raised(on_path: OnPath) -> None:
    """`doctor` runs when things are broken; a hung tool is one of them."""
    on_path("helm")

    class Hanging:
        def run(self, args: Sequence[str], **kwargs: Any) -> CommandResult:
            raise CommandTimeout(f"command timed out: {' '.join(args)}")

    check = probe_binary(Hanging(), "helm", name="helm", remediation="install helm")

    assert check.status is CheckStatus.FAILED
    assert check.outcome is Outcome.TOOL


def test_a_probe_is_always_capped(on_path: OnPath) -> None:
    """No probe may inherit the unbounded default `Settings.command_timeout`."""
    on_path("helm")
    runner = FakeCommandRunner(stdout="v3\n")

    probe_binary(runner, "helm", name="helm", remediation="install helm")

    assert runner.records[0].timeout == PROBE_TIMEOUT


def test_presence_only_probes_skip_the_subprocess(on_path: OnPath) -> None:
    """Some tools have no version flag; asking anyway would report them broken."""
    on_path("renovate-config-validator")
    runner = FakeCommandRunner(when_exhausted="raise")

    check = probe_binary(
        runner,
        "renovate-config-validator",
        name="renovate-config-validator",
        version_args=(),
        remediation="npm install -g renovate",
    )

    assert check.status is CheckStatus.OK
    assert runner.calls == []


# --- checks belong to the integration that owns the tool --------------------


def test_helm_probes_the_binary_it_actually_resolved(on_path: OnPath) -> None:
    """A preflight against a different helm than the adapter uses is worthless."""
    on_path(f"{_BIN}/mise/helm")
    runner = FakeCommandRunner(stdout="v3.16.2\n")

    checks = Helm(runner, binary=f"{_BIN}/mise/helm").preflight()

    assert _by_name(checks)["helm"].status is CheckStatus.OK
    assert runner.calls[0][0] == f"{_BIN}/mise/helm"


def test_kubectl_reports_the_ambient_context(on_path: OnPath) -> None:
    """No pin: the check answers with whatever the kubeconfig points at."""
    on_path("kubectl")
    runner = FakeCommandRunner()
    runner.respond(("kubectl", "version"), stdout='{"clientVersion":{"gitVersion":"v1.31.0"}}')
    runner.respond(("kubectl", "config", "current-context"), stdout="kind-lab\n")

    checks = _by_name(Kubectl(runner).preflight())

    assert checks["kubectl"].detail.startswith("v1.31.0")
    assert checks["kube-context"].status is CheckStatus.OK
    assert "kind-lab" in checks["kube-context"].detail


def test_no_current_kubecontext_is_an_environment_failure(on_path: OnPath) -> None:
    """Exit 5, per the table: nothing is missing, the environment is unset."""
    on_path("kubectl")
    runner = FakeCommandRunner()
    runner.respond(("kubectl", "version"), stdout='{"clientVersion":{"gitVersion":"v1.31.0"}}')
    runner.respond(("kubectl", "config", "current-context"), returncode=1)

    context = _by_name(Kubectl(runner).preflight())["kube-context"]

    assert context.status is CheckStatus.FAILED
    assert context.outcome is Outcome.ENVIRONMENT


def test_a_pinned_context_missing_from_the_kubeconfig_fails(on_path: OnPath) -> None:
    """`CHART_MANAGER_KUBE_CONTEXT` naming a context nobody has is a real bug."""
    on_path("kubectl")
    runner = FakeCommandRunner()
    runner.respond(("kubectl", "version"), stdout='{"clientVersion":{"gitVersion":"v1.31.0"}}')
    runner.respond(("kubectl", "config", "get-contexts"), stdout="kind-lab\nprod\n")

    context = _by_name(Kubectl(runner, context="kind-gone").preflight())["kube-context"]

    assert context.status is CheckStatus.FAILED
    assert context.outcome is Outcome.ENVIRONMENT
    assert "kind-lab" in (context.remediation or ""), "say which contexts do exist"


def test_the_context_check_is_skipped_when_kubectl_is_absent(on_path: OnPath) -> None:
    """One broken install, one line of blame -- not two."""
    on_path()

    checks = _by_name(Kubectl(FakeCommandRunner()).preflight())

    assert checks["kubectl"].outcome is Outcome.MISSING_BINARY
    assert checks["kube-context"].status is CheckStatus.SKIPPED
    assert checks["kube-context"].outcome is Outcome.SUCCESS, "a skip is not a failure"


def test_a_stopped_docker_daemon_is_reported_not_a_missing_binary(on_path: OnPath) -> None:
    """The common case a binary-only check calls healthy."""
    on_path("kind", "docker")
    runner = FakeCommandRunner()
    runner.respond(("kind", "version"), stdout="kind v0.24.0\n")
    runner.respond(("docker", "--version"), stdout="Docker version 27.3.1\n")
    runner.respond(
        ("docker", "version", "--format"),
        returncode=1,
        stderr="Cannot connect to the Docker daemon\n",
    )

    checks = _by_name(Kind(runner).preflight())

    assert checks["kind"].status is CheckStatus.OK
    assert checks["docker"].status is CheckStatus.OK
    assert checks["docker-daemon"].outcome is Outcome.ENVIRONMENT


def test_the_daemon_probe_is_scoped_to_the_configured_docker_host(on_path: OnPath) -> None:
    """A preflight against the ambient daemon says nothing about the pinned one."""
    on_path("kind", "docker")
    runner = FakeCommandRunner(stdout="27.3.1\n")

    Kind(runner, docker_host="tcp://remote:2375").preflight()

    daemon_call = next(r for r in runner.records if r.args[:2] == ("docker", "version"))
    assert daemon_call.env == {"DOCKER_HOST": "tcp://remote:2375"}
    assert daemon_call.timeout == PROBE_TIMEOUT


def test_kubeconform_and_kyverno_own_their_own_version_flags(on_path: OnPath) -> None:
    """The surface must never learn that one spells it `-v` and the other `version`."""
    on_path("kubeconform", "kyverno")
    runner = FakeCommandRunner(stdout="v0.6.7\n")

    Kubeconform(runner).preflight()
    Kyverno(runner).preflight()

    assert ("kubeconform", "-v") in runner.calls
    assert ("kyverno", "version") in runner.calls


def test_renovate_reports_a_missing_token_as_environment(
    on_path: OnPath, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Required configuration is a per-integration preflight matter (MY_COMMENTS.md)."""
    on_path("renovate", "renovate-config-validator")
    for variable in ("RENOVATE_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(variable, raising=False)

    token = _by_name(Renovate(FakeCommandRunner(stdout="40.0.0\n")).preflight())["renovate-token"]

    assert token.status is CheckStatus.FAILED
    assert token.outcome is Outcome.ENVIRONMENT


def test_renovate_accepts_the_ci_token_it_actually_falls_back_to(
    on_path: OnPath, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composition root reads GITHUB_TOKEN as the fallback; so must the check."""
    on_path("renovate", "renovate-config-validator")
    monkeypatch.delenv("RENOVATE_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_fake")

    token = _by_name(Renovate(FakeCommandRunner(stdout="40.0.0\n")).preflight())["renovate-token"]

    assert token.status is CheckStatus.OK


#: A real `gh auth status` report: a bare host header, then marked per-account
#: lines. Verbatim because the shape is the whole reason `_status_line` exists.
_GH_AUTH_FAILURE = (
    "github.com\n"
    "  X Failed to log in to github.com using token (GITHUB_TOKEN)\n"
    "  - The token in GITHUB_TOKEN is invalid.\n"
)


def test_unauthenticated_gh_is_an_environment_failure(on_path: OnPath, tmp_path: Path) -> None:
    """`gh` installed predicts nothing about whether a promote can open a PR."""
    on_path("gh")
    runner = FakeCommandRunner()
    runner.respond(("gh", "--version"), stdout="gh version 2.62.0\n")
    runner.respond(("gh", "auth", "status"), returncode=1, stderr=_GH_AUTH_FAILURE)

    auth = _by_name(Github(tmp_path, runner).preflight())["gh-auth"]

    assert auth.status is CheckStatus.FAILED
    assert auth.outcome is Outcome.ENVIRONMENT
    assert "Failed to log in" in auth.detail, "the host header alone is not a diagnostic"


def test_a_root_that_is_not_a_checkout_is_reported(on_path: OnPath, tmp_path: Path) -> None:
    """Indistinguishable from "nothing changed" at every CI selector otherwise."""
    on_path("git")
    runner = FakeCommandRunner()
    runner.respond(("git", "--version"), stdout="git version 2.47.0\n")
    runner.respond(("git", "rev-parse"), returncode=128, stderr="not a git repository\n")

    repository = _by_name(Git(tmp_path, runner).preflight())["git-repository"]

    assert repository.status is CheckStatus.FAILED
    assert repository.outcome is Outcome.ENVIRONMENT


# --- the events backend -----------------------------------------------------


def test_events_disabled_is_a_skip_and_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """`EVENTS_BACKEND=none` is a supported configuration, so `doctor` stays green."""
    monkeypatch.setenv("EVENTS_BACKEND", "none")

    (check,) = preflight_event_store()

    assert check.status is CheckStatus.SKIPPED
    assert check.outcome is Outcome.SUCCESS


def test_an_unknown_events_backend_is_a_spec_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing is down: the operator wrote something the switch does not accept."""
    monkeypatch.setenv("EVENTS_BACKEND", "postgres")

    (check,) = preflight_event_store()

    assert check.outcome is Outcome.SPEC
    assert "postgres" in check.detail


def test_an_unconfigured_cosmos_backend_reports_config_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naming the unset variable beats "connection refused" from an SDK."""
    monkeypatch.setenv("EVENTS_BACKEND", "cosmos")
    for variable in ("COSMOS_CONNECTION_STRING", "COSMOS_ENDPOINT"):
        monkeypatch.delenv(variable, raising=False)

    (check,) = preflight_event_store()

    assert check.outcome is Outcome.ENVIRONMENT
    assert "COSMOS_ENDPOINT" in check.detail


# --- the fold ---------------------------------------------------------------


def _provider(*checks: Check) -> Callable[[], Sequence[Check]]:
    """A fake capability that reports exactly `checks`."""
    return lambda: checks


_OK = Check.ok("fine", "1.0")
_MISSING = Check.failed(
    "helm", "not on PATH", remediation="install helm", outcome=Outcome.MISSING_BINARY
)
_UNREACHABLE = Check.failed(
    "events-backend", "down", remediation="start it", outcome=Outcome.ENVIRONMENT
)


def test_an_all_green_run_is_success() -> None:
    """The zero-failure case is the one an exit-code bug would hide."""
    report = DoctorService({"a": _provider(_OK), "b": _provider(_OK)}).run()

    assert report.ok
    assert report.outcome is Outcome.SUCCESS
    assert len(report.checks) == 2


def test_a_skipped_check_does_not_fail_the_run() -> None:
    """Otherwise "no docker, so no daemon check" would exit nonzero twice over."""
    report = DoctorService({"a": _provider(_OK, Check.skipped("x", "n/a"))}).run()

    assert report.ok
    assert report.outcome is Outcome.SUCCESS


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ((_MISSING,), Outcome.MISSING_BINARY),
        ((_UNREACHABLE,), Outcome.ENVIRONMENT),
        ((_UNREACHABLE, _MISSING), Outcome.MISSING_BINARY),
    ],
    ids=["missing-binary", "unreachable-backend", "both-picks-the-binary"],
)
def test_the_aggregate_outcome_follows_the_documented_precedence(
    checks: tuple[Check, ...], expected: Outcome
) -> None:
    """One process, one exit code -- so a mixed run has to pick, deterministically."""
    report = DoctorService({"a": _provider(*checks)}).run()

    assert not report.ok
    assert report.outcome is expected


def test_report_order_is_the_composition_roots_order() -> None:
    """Insertion order, not sorted: the container decides how the table reads."""
    service = DoctorService(
        {
            "zeta": _provider(Check.ok("zeta", "")),
            "alpha": _provider(Check.ok("alpha", "")),
        }
    )

    assert [check.name for check in service.run().checks] == ["zeta", "alpha"]


def test_for_narrows_to_the_capabilities_that_command_needs() -> None:
    """`--for chart validate` must not probe docker or the events backend."""
    service = DoctorService(
        {
            "helm": _provider(Check.ok("helm", "")),
            "kubeconform": _provider(Check.ok("kubeconform", "")),
            "kyverno": _provider(Check.ok("kyverno", "")),
            "kind": _provider(Check.ok("kind", "")),
            "events": _provider(Check.ok("events-backend", "")),
        }
    )

    report = service.run(for_command="chart validate")

    assert {check.name for check in report.checks} == {"helm", "kubeconform", "kyverno"}
    assert report.selector == "chart validate"


def test_a_capability_with_no_requirements_runs_nothing() -> None:
    """"This command shells out to nothing" is a real answer, not an error."""
    report = DoctorService({"helm": _provider(_MISSING)}).run(for_command="chart list")

    assert report.checks == ()
    assert report.ok


def test_a_provider_that_raises_becomes_a_failed_check_not_a_crash() -> None:
    """One broken adapter must not cost the operator the other nine answers."""

    def explode() -> Sequence[Check]:
        raise RuntimeError("boom")

    report = DoctorService({"a": explode, "b": _provider(_OK)}).run()

    assert not report.ok
    assert report.outcome is Outcome.ENVIRONMENT
    assert "RuntimeError" in report.checks[0].detail
    assert report.checks[1] == _OK, "the surviving provider still reported"


# --- the wire shape and the requirement table -------------------------------


def test_the_json_document_has_a_stable_shape() -> None:
    """`-o json` is a contract; this is it, written down."""
    payload = DoctorService({"a": _provider(_MISSING, _OK)}).run().to_dict()

    assert payload == {
        "ok": False,
        "outcome": "missing-binary",
        "for": None,
        "checks": [
            {
                "name": "helm",
                "status": "failed",
                "detail": "not on PATH",
                "remediation": "install helm",
            },
            {"name": "fine", "status": "ok", "detail": "1.0", "remediation": None},
        ],
    }


def test_every_for_target_names_a_command_the_app_registers() -> None:
    """A rename must not leave `--for` offering a command nobody can run.

    Same guard, and the same reasoning, as
    `tests/test_cli_argv_table.py::test_every_rewrite_target_resolves_against_the_app`:
    a table of strings is not type-checked, and a stale entry here fails as
    a confusing usage error rather than as one clear message.
    """
    registered: set[str] = set()
    pending: list[tuple[Any, tuple[str, ...]]] = [(typer.main.get_command(_root_app()), ())]
    while pending:
        command, prefix = pending.pop()
        children = getattr(command, "commands", {})
        if not children and prefix:
            registered.add(" ".join(prefix))
        for name, child in children.items():
            pending.append((child, (*prefix, name)))

    dangling = sorted(set(COMMAND_REQUIREMENTS) - registered)
    assert not dangling, (
        "services/doctor.py::COMMAND_REQUIREMENTS names commands the CLI does not "
        f"register: {dangling}"
    )


def test_every_required_capability_is_wired_in_the_container() -> None:
    """The other direction: a requirement no provider satisfies is silently dropped."""
    from chart_manager.composition import Container

    wired = set(Container().doctor_service().capabilities())
    required = {name for names in COMMAND_REQUIREMENTS.values() for name in names}

    assert required <= wired, f"unwired capabilities: {sorted(required - wired)}"
