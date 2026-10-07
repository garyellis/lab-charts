"""`chart-manager promote pr|monitor|test`: flags, output, exit codes, with each `run` patched."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import typer

from chart_manager.commands.promote import cli as promote_cli
from chart_manager.commands.promote.monitor import MonitorOutcome, MonitorResult
from chart_manager.commands.promote.pr import PromoteResult
from chart_manager.commands.promote.scanner import HelmReleaseMatch
from chart_manager.commands.promote.state import (
    NO_MATCH_REF,
    PROMOTE_OUTCOME,
    PromoteStatus,
    Transition,
)
from chart_manager.commands.promote.test import TestOutcome, TestResult
from chart_manager.integrations.github import PullRequest
from chart_manager.integrations.kubectl import (
    ConditionSnapshot,
    HelmReleaseRef,
    HelmReleaseStatus,
)
from tests.conftest import cli

# ----- helpers ------------------------------------------------------------


def _ref(name: str = "loki", ns: str = "loki") -> HelmReleaseRef:
    return HelmReleaseRef(
        name=name,
        namespace=ns,
        api_version="helm.toolkit.fluxcd.io/v2",
        release_name=name,
        storage_namespace=ns,
        target_namespace=ns,
    )


def _status(ref: HelmReleaseRef) -> HelmReleaseStatus:
    return HelmReleaseStatus(
        ref=ref,
        observed_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        generation=1,
        observed_generation=1,
        resource_version="1",
        suspended=False,
        desired_chart_name="loki",
        desired_chart_version="0.2.0",
        last_applied_revision=None,
        history_chart_version="0.2.0",
        conditions=(
            ConditionSnapshot(
                type="Ready",
                status="True",
                reason="ReconciliationSucceeded",
                message="ok",
                last_transition_time=None,
            ),
        ),
    )


def _ready_outcome(ref: HelmReleaseRef) -> MonitorOutcome:
    return MonitorOutcome(
        ref=ref,
        verdict="ready",
        reason="Ready",
        last_status=_status(ref),
        last_workloads=(),
        recent_transitions=(),
        diagnostics=None,
        duration_seconds=1.5,
    )


def _failed_outcome(ref: HelmReleaseRef) -> MonitorOutcome:
    return MonitorOutcome(
        ref=ref,
        verdict="failed",
        reason="InstallFailed",
        last_status=_status(ref),
        last_workloads=(),
        recent_transitions=(),
        diagnostics="## loki/loki - failed: InstallFailed\nbad chart values",
        duration_seconds=3.2,
    )


def _passed_test_outcome(ref: HelmReleaseRef) -> TestOutcome:
    return TestOutcome(
        ref=ref,
        verdict="passed",
        reason="AllTestsPassed",
        helm_test_returncode=0,
        helm_test_stdout="PASS",
        helm_test_stderr="",
        test_pods=(),
        last_status=_status(ref),
        phase_log=(),
        diagnostics=None,
        duration_seconds=2.0,
    )


@dataclass
class _FakeStage:
    """Stands in for a stage's `run()`: records each request and progress callback."""

    result: Any
    raise_exc: BaseException | None = None
    captured_requests: list[Any] = field(default_factory=list)
    captured_progress: list[Any] = field(default_factory=list)

    def __call__(self, request: Any, *, progress: Any, **_adapters: Any) -> Any:
        self.captured_requests.append(request)
        self.captured_progress.append(progress)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


def _install_fake_monitor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: MonitorResult,
    raise_exc: BaseException | None = None,
) -> _FakeStage:
    fake = _FakeStage(result, raise_exc)
    monkeypatch.setattr(promote_cli, "run_monitor", fake)
    return fake


def _install_fake_test(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: TestResult,
    raise_exc: BaseException | None = None,
) -> _FakeStage:
    fake = _FakeStage(result, raise_exc)
    monkeypatch.setattr(promote_cli, "run_test", fake)
    return fake


def _ok_result(outcomes: tuple[MonitorOutcome, ...] = ()) -> MonitorResult:
    if not outcomes:
        outcomes = (_ready_outcome(_ref()),)
    return MonitorResult(outcomes=outcomes, total_duration_seconds=1.2, total_timed_out=False)


def _bad_result() -> MonitorResult:
    return MonitorResult(
        outcomes=(_failed_outcome(_ref()),),
        total_duration_seconds=3.5,
        total_timed_out=False,
    )


@pytest.fixture(autouse=True)
def _clear_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)


_BASE = ["promote", "monitor", "--chart", "loki", "--version", "0.2.0"]


# ----- required option tests ----------------------------------------------


@pytest.mark.parametrize("missing", ["--chart", "--version"])
def test_chart_and_version_are_required(missing: str) -> None:
    at = _BASE.index(missing)
    assert cli(*_BASE[:at], *_BASE[at + 2 :]).exit_code == 2


# ----- pretty / json modes -------------------------------------------------


def test_pretty_ok_exit_0_summary_in_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_monitor(monkeypatch, result=_ok_result())
    res = cli(*_BASE, "--output", "table")
    assert res.exit_code == 0
    # diagnostics never written for ready outcomes
    assert "InstallFailed" not in res.stdout
    assert "ready" in res.stdout
    # CliRunner is non-tty; an explicit table must not be coerced to json.
    assert not res.stdout.lstrip().startswith("{")


def test_pretty_failure_exit_1_diagnostics_in_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_monitor(monkeypatch, result=_bad_result())
    res = cli(*_BASE, "--output", "table")
    assert res.exit_code == 1
    assert "InstallFailed" in res.stdout


def test_json_mode_emits_parseable_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_monitor(monkeypatch, result=_ok_result())
    res = cli(*_BASE, "--output", "json")
    assert res.exit_code == 0
    assert res.stdout.endswith("\n")
    payload = json.loads(res.stdout)
    assert payload["command"] == "monitor"
    assert payload["ok"] is True
    # No ANSI escapes leaked into json stream.
    assert "\x1b[" not in res.stdout


# ----- progress wiring -----------------------------------------------------


def test_table_mode_renders_progress_on_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(_request: Any, *, progress: Any, **_adapters: Any) -> MonitorResult:
        # Watchers report from their own threads, 20 transitions each.
        def watch(i: int) -> None:
            for j in range(20):
                progress(_ref(f"hr{i}", "ns"), Transition(datetime.now(UTC), "Polling", f"d{j}"))

        threads = [threading.Thread(target=watch, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return _ok_result()

    monkeypatch.setattr(promote_cli, "run_monitor", run)
    res = cli(*_BASE, "--output", "table")
    assert res.exit_code == 0
    assert all(f"hr{i}" in res.stderr for i in range(5))
    assert "Polling" not in res.stdout


def test_json_mode_omits_progress_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_monitor(monkeypatch, result=_ok_result())
    res = cli(*_BASE, "--output", "json")
    assert res.exit_code == 0
    assert fake.captured_progress[0] is None


# ----- auto mode resolution ------------------------------------------------


def test_auto_mode_under_ci_env_picks_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    _install_fake_monitor(monkeypatch, result=_ok_result())
    res = cli(*_BASE, "--output", "auto")
    assert res.exit_code == 0
    assert json.loads(res.stdout)["ok"] is True


# ----- namespace coercion --------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [("", None), ("obs", "obs")])
def test_namespace_reaches_the_request_with_empty_meaning_all(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str | None
) -> None:
    fake = _install_fake_monitor(monkeypatch, result=_ok_result())
    res = cli(*_BASE, "--namespace", value)
    assert res.exit_code == 0
    assert fake.captured_requests[0].namespace == expected


# ----- json output -----------------------------------------------------------


def test_json_schema_matches_expected_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    ref_ready = _ref("a", "ns1")
    ref_failed = _ref("b", "ns2")
    ref_timeout = _ref("c", "ns3")
    failed = _failed_outcome(ref_failed)
    timeout = MonitorOutcome(
        ref=ref_timeout,
        verdict="timed-out",
        reason="PerHRBudgetExhausted",
        last_status=None,
        last_workloads=(),
        recent_transitions=(),
        diagnostics="## ns3/c - timed-out: PerHRBudgetExhausted",
        duration_seconds=300.0,
    )
    result = MonitorResult(
        outcomes=(_ready_outcome(ref_ready), failed, timeout),
        total_duration_seconds=305.0,
        total_timed_out=False,
    )
    _install_fake_monitor(monkeypatch, result=result)
    res = cli(*_BASE, "--output", "json")
    assert res.exit_code == 1
    payload = json.loads(res.stdout)
    verdicts = [o["verdict"] for o in payload["outcomes"]]
    assert verdicts == ["ready", "failed", "timed-out"]
    assert payload["outcomes"][1]["diagnostics"]
    assert payload["outcomes"][2]["reason"] == "PerHRBudgetExhausted"
    assert payload["ok"] is False


# ----- test (helm test) command -------------------------------------------


def test_test_pod_log_tail_plumbed(monkeypatch: pytest.MonkeyPatch) -> None:
    result = TestResult(
        outcomes=(_passed_test_outcome(_ref()),),
        total_duration_seconds=2.0,
        total_timed_out=False,
    )
    fake = _install_fake_test(monkeypatch, result=result)
    res = cli(*_TEST_BASE, "--pod-log-tail", "50")
    assert res.exit_code == 0
    assert fake.captured_requests[0].pod_log_tail == 50


def test_monitor_fail_fast_plumbed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_monitor(monkeypatch, result=_ok_result())
    res = cli(*_BASE, "--fail-fast")
    assert res.exit_code == 0
    assert fake.captured_requests[0].fail_fast is True


# ----- timeout options: parsed once, at the CLI boundary -------------------


_TEST_BASE = ["promote", "test", "--chart", "loki", "--version", "0.2.0"]


def _passed_test_result() -> TestResult:
    return TestResult(
        outcomes=(_passed_test_outcome(_ref()),),
        total_duration_seconds=2.0,
        total_timed_out=False,
    )


def _timeouts(request: Any) -> tuple[float, float, float]:
    return (
        request.per_poll_timeout_seconds,
        request.per_hr_timeout_seconds,
        request.total_timeout_seconds,
    )


@pytest.mark.parametrize("command", ["monitor", "test"])
@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ((), (10.0, 300.0, 900.0)),
        (
            ("--per-poll-timeout", "2.5s", "--per-hr-timeout", "90", "--total-timeout", "1h"),
            (2.5, 90.0, 3600.0),
        ),
    ],
)
def test_timeouts_reach_run_as_seconds(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    flags: tuple[str, ...],
    expected: tuple[float, float, float],
) -> None:
    monitor_fake = _install_fake_monitor(monkeypatch, result=_ok_result())
    test_fake = _install_fake_test(monkeypatch, result=_passed_test_result())
    res = cli(*(_BASE if command == "monitor" else _TEST_BASE), *flags)
    assert res.exit_code == 0, res.output
    fake = monitor_fake if command == "monitor" else test_fake
    assert _timeouts(fake.captured_requests[0]) == expected


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--per-poll-timeout", "abc"),
        ("--per-hr-timeout", "abc"),
        ("--total-timeout", "abc"),
        # Non-finite values ride the same path; the full range of bad values
        # is covered against `parse_duration` in test_plumbing_duration.py.
        ("--total-timeout", "nan"),
    ],
)
def test_malformed_timeout_is_a_usage_error_before_any_run(
    monkeypatch: pytest.MonkeyPatch, flag: str, value: str
) -> None:
    fake = _install_fake_monitor(monkeypatch, result=_ok_result())
    res = cli(*_BASE, flag, value)
    # Exit 2 is click's usage-error code; the message names the flag and no
    # traceback reaches the operator.
    assert res.exit_code == 2, res.output
    assert flag in res.stderr
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert fake.captured_requests == []


# ----- no-match outcome rendering -----------------------------------------


def test_no_match_outcome_pretty_message(monkeypatch: pytest.MonkeyPatch) -> None:
    no_match = MonitorOutcome(
        ref=NO_MATCH_REF,
        verdict="no-match",
        reason="NoHelmReleasesMatched",
        last_status=None,
        last_workloads=(),
        recent_transitions=(),
        diagnostics=None,
        duration_seconds=0.1,
    )
    result = MonitorResult(
        outcomes=(no_match,), total_duration_seconds=0.1, total_timed_out=False
    )
    _install_fake_monitor(monkeypatch, result=result)
    res = cli(*_BASE, "--output", "table")
    assert res.exit_code == 1
    assert "no helmreleases matched" in res.stdout


# ----- promote: exit codes, non-interactive guard, json projection --------
#
# The defect these lock down: `promote` decoded its six
# terminal states into six `console.print` calls and raised `typer.Exit` on
# none of them. A declined downgrade printed "aborted ... no PR opened" and
# exited 0, so a promotion that did nothing was indistinguishable from one
# that opened a PR -- and the decline itself was usually not a human choice
# but `typer.confirm` reading EOF on a non-TTY runner.


_PROMOTE_BASE = [
    "promote", "pr",
    "--flux-repo", "git@github.com:org/lab-fluxcd.git",
    "--path", "prod",
    "--env", "prod",
    "--chart", "loki",
    "--version", "0.2.0",
]


def _match(current: str = "0.1.0") -> HelmReleaseMatch:
    return HelmReleaseMatch(
        path=Path("/tmp/flux/prod/loki.yaml"),
        doc_index=0,
        name="loki",
        namespace="loki",
        current_version=current,
    )


def _promote_result(status: PromoteStatus) -> PromoteResult:
    """A plausible PromoteResult for each terminal state.

    Shaped like what `pr.run` actually returns
    for that state, so an exit-code assertion is not passing against a
    result `pr.run` could never produce.
    """
    match status:
        case PromoteStatus.NO_CHANGES:
            return PromoteResult(status=status, matches=[_match("0.2.0")])
        case PromoteStatus.DRY_RUN:
            return PromoteResult(
                status=status,
                matches=[_match()],
                changed_files=[Path("/tmp/flux/prod/loki.yaml")],
                branch="promote/prod/loki-0.2.0",
            )
        case PromoteStatus.ABORTED:
            return PromoteResult(
                status=status,
                matches=[_match("0.9.0")],
                branch="promote/prod/loki-0.2.0",
                downgrades=[_match("0.9.0")],
            )
        case PromoteStatus.ALREADY_OPEN:
            return PromoteResult(
                status=status,
                matches=[_match()],
                branch="promote/prod/loki-0.2.0",
                pull_request=PullRequest(url="https://gh/org/r/pull/7", number=7),
            )
        case PromoteStatus.PR_OPENED:
            return PromoteResult(
                status=status,
                matches=[_match()],
                changed_files=[Path("/tmp/flux/prod/loki.yaml")],
                branch="promote/prod/loki-0.2.0",
                pull_request=PullRequest(url="https://gh/org/r/pull/8", number=8),
            )
        case PromoteStatus.PUSHED:
            return PromoteResult(
                status=status,
                matches=[_match()],
                changed_files=[Path("/tmp/flux/prod/loki.yaml")],
                branch="promote/prod/loki-0.2.0",
                pull_request=PullRequest(url="", number=None),
            )


def _install_fake_promote(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: PromoteResult | None = None,
    downgrades: list[HelmReleaseMatch] | None = None,
) -> None:
    """Patch `pr.run` to replay `result`, consulting the downgrade callback as it does."""

    def fake(request: Any, *, confirm_downgrade: Any, **_adapters: Any) -> PromoteResult:
        if downgrades and not confirm_downgrade(downgrades, request.version):
            return _promote_result(PromoteStatus.ABORTED)
        assert result is not None
        return result

    monkeypatch.setattr(promote_cli, "run_pr", fake)


@pytest.mark.parametrize("status", list(PromoteStatus))
def test_promote_exit_code_and_json_ok_per_status(
    status: PromoteStatus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a declined downgrade fails, and `.ok` and `$?` always agree."""
    _install_fake_promote(monkeypatch, result=_promote_result(status))
    res = cli(*_PROMOTE_BASE, "--output", "json")
    expected = 1 if status is PromoteStatus.ABORTED else 0
    assert res.exit_code == expected, res.output
    assert json.loads(res.stdout)["ok"] is (expected == 0)


def test_promote_outcome_covers_every_status() -> None:
    """A new PromoteStatus without an outcome must fail here, not exit 0 at runtime."""
    assert set(PROMOTE_OUTCOME) == set(PromoteStatus)


def test_promote_aborted_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """The headline regression: a declined downgrade is a failure, not success."""
    _install_fake_promote(monkeypatch, result=_promote_result(PromoteStatus.ABORTED))
    res = cli(*_PROMOTE_BASE, "--output", "table")
    assert res.exit_code == 1
    assert "aborted" in res.stderr


# ----- promote: the non-interactive downgrade guard ------------------------


def test_promote_downgrade_without_flag_is_a_usage_error_when_non_interactive(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """No prompt, exit 2, and the message names the flag that unblocks it.

    CliRunner's stdin is not a terminal, which is the condition a CI runner
    presents. Previously this reached `typer.confirm`, EOF'd into a decline,
    and exited 0.
    """
    prompted: list[str] = []

    def _boom(*_a: Any, **_k: Any) -> bool:
        prompted.append("prompted")
        return False

    monkeypatch.setattr(typer, "confirm", _boom)
    _install_fake_promote(monkeypatch, downgrades=[_match("0.9.0")])

    res = cli(*_PROMOTE_BASE)

    assert res.exit_code == 2, res.output
    assert prompted == [], "must never prompt when stdin is not a terminal"
    assert "--allow-downgrade" in res.stderr


def test_promote_downgrade_guard_also_trips_on_ci_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI=true is non-interactive even where a pty exists (6.6, both legs)."""
    monkeypatch.setenv("CI", "true")
    _install_fake_promote(monkeypatch, downgrades=[_match("0.9.0")])

    # `input=` makes stdin readable, so a "y" is waiting. The guard must
    # still refuse: CI=true means nobody typed it.
    res = cli(*_PROMOTE_BASE, input="y\n")

    assert res.exit_code == 2, res.output
    assert "--allow-downgrade" in res.stderr


def test_promote_allow_downgrade_skips_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """--allow-downgrade is the documented escape, so it must not hit the guard."""
    _install_fake_promote(
        monkeypatch,
        result=_promote_result(PromoteStatus.PR_OPENED),
        downgrades=[_match("0.9.0")],
    )
    res = cli(*_PROMOTE_BASE, "--allow-downgrade")

    assert res.exit_code == 0, res.output
    assert "--allow-downgrade set; proceeding." in res.stderr


def test_promote_still_prompts_when_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard the guard: the interactive path is gated, not deleted."""
    monkeypatch.setattr(promote_cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)
    _install_fake_promote(
        monkeypatch,
        result=_promote_result(PromoteStatus.PR_OPENED),
        downgrades=[_match("0.9.0")],
    )
    res = cli(*_PROMOTE_BASE)

    assert res.exit_code == 0, res.output


def test_promote_interactive_decline_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """A human who says no gets exit 1 -- the state that used to exit 0."""
    monkeypatch.setattr(promote_cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)
    _install_fake_promote(monkeypatch, downgrades=[_match("0.9.0")])
    res = cli(*_PROMOTE_BASE)

    assert res.exit_code == 1, res.output


# ----- promote: the json projection ---------------------------------------


def test_promote_json_parses_cleanly_off_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout carries the projection and nothing else.

    An *explicit* `--output json` also silences narration ("json
    implies --quiet"), so stderr is empty here. The stdout purity
    is still checked by `json.loads` below.

    The companion property -- that the narration still exists and is merely
    suppressed -- is held by
    `test_promote_auto_json_keeps_narration_on_stderr`, which reaches the same
    json projection through `auto` rather than by asking for it. Without that
    sibling this test would pass just as well against a promote that had
    stopped narrating entirely.
    """
    _install_fake_promote(monkeypatch, result=_promote_result(PromoteStatus.PR_OPENED))
    res = cli(*_PROMOTE_BASE, "--output", "json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["command"] == "promote"
    assert payload["status"] == "pr-opened"
    assert payload["ok"] is True
    assert payload["environment"] == "prod"
    assert payload["chart"] == "loki"
    assert payload["pull_request"]["url"] == "https://gh/org/r/pull/8"
    assert "pr opened" not in res.stdout
    assert res.stderr == ""


def test_promote_auto_json_keeps_narration_on_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """`auto` resolving to json is a format decision, not a request for silence.

    CliRunner's stdout is not a terminal, so no `--output` flag resolves to
    json here -- the same thing that happens for every command in CI. The
    projection must still be clean, and the operator commentary must still be
    on stderr: `promote pr` reports a *mutation*, and a CI log that
    shows the PR was opened is the reason that narration exists.
    """
    _install_fake_promote(monkeypatch, result=_promote_result(PromoteStatus.PR_OPENED))
    res = cli(*_PROMOTE_BASE)

    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["status"] == "pr-opened"
    assert "pr opened" in res.stderr
    assert "pr opened" not in res.stdout


def test_promote_json_carries_a_failure_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nonzero exit still emits a parseable document -- CI needs both."""
    _install_fake_promote(monkeypatch, result=_promote_result(PromoteStatus.ABORTED))
    res = cli(*_PROMOTE_BASE, "--output", "json")

    assert res.exit_code == 1
    payload = json.loads(res.stdout)
    assert payload["ok"] is False
    assert payload["status"] == "aborted"
    assert payload["downgrades"][0]["current_version"] == "0.9.0"


def test_promote_pretty_writes_nothing_to_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Promote narrates a mutation; it has no human *document*.

    So `promote pr --output table >/dev/null` still shows the
    operator what happened, and no status line is ever piped to a consumer
    that asked for data.
    """
    _install_fake_promote(monkeypatch, result=_promote_result(PromoteStatus.PR_OPENED))
    res = cli(*_PROMOTE_BASE, "--output", "table")

    assert res.exit_code == 0, res.output
    assert res.stdout == ""
    assert "pr opened" in res.stderr


def test_promote_rejects_an_unknown_output_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--output yaml` must fail, not silently fall back to table.

    Exit 2, not 1: naming a projection a command does not have is a *usage*
    error, and every `--output` shares one rejection path
    (`typer.BadParameter`, via `cli/output.py`).
    """
    _install_fake_promote(monkeypatch, result=_promote_result(PromoteStatus.PR_OPENED))
    res = cli(*_PROMOTE_BASE, "--output", "yaml")

    assert res.exit_code == 2
    assert "yaml" in res.output
    assert "table" in res.output
