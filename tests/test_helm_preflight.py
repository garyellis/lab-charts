"""`Helm.preflight` and the shared binary probe it runs."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.commands import CommandResult
from chart_manager.plumbing.errors import CommandTimeout
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import PROBE_TIMEOUT, CheckStatus, probe_binary
from tests.conftest import FAKE_BIN, FakeCommandRunner, OnPath, checks_by_name


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
    assert f"{FAKE_BIN}/helm" in check.detail


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


def test_helm_probes_the_binary_it_actually_resolved(on_path: OnPath) -> None:
    """A preflight against a different helm than the adapter uses is worthless."""
    on_path(f"{FAKE_BIN}/mise/helm")
    runner = FakeCommandRunner(stdout="v3.16.2\n")

    checks = Helm(
        runner, binary=f"{FAKE_BIN}/mise/helm", timeout=None, context=None
    ).preflight()

    assert checks_by_name(checks)["helm"].status is CheckStatus.OK
    assert runner.calls[0][0] == f"{FAKE_BIN}/mise/helm"
