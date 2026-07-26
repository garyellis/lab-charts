"""CLI tests for `chart-manager helmrelease monitor|test|promote`."""
from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from chart_manager.cli import helmrelease as helmrelease_cli
from chart_manager.cli.helmrelease_render import (
    _PrettyProgressDriver,
    render_monitor_json,
    render_test_json,
)
from chart_manager.integrations.helmrelease import (
    ConditionSnapshot,
    HelmReleaseRef,
    HelmReleaseStatus,
    OwnedWorkload,
    WorkloadRollout,
)
from chart_manager.services.helmrelease import (
    NO_MATCH_REF,
    MonitorOutcome,
    MonitorResult,
    TestOutcome,
    TestPodSnapshot,
    TestResult,
    Transition,
)

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"
MONITOR_JSON_GOLDEN = GOLDEN_DIR / "helmrelease-monitor.json.golden"
TEST_JSON_GOLDEN = GOLDEN_DIR / "helmrelease-test.json.golden"

# ----- helpers ------------------------------------------------------------


def _build_app() -> typer.Typer:
    """Build a typer app that mirrors main()'s ChartManagerError -> stderr+exit-1 mapping.

    The real CLI entrypoint catches ChartManagerError in main() and prints
    a stable stderr message. To exercise sub-commands through CliRunner
    while preserving that mapping, we wrap each registered handler with a
    catcher that emits the same `error:` line and re-raises as typer.Exit(1).
    """
    from chart_manager.plumbing.errors import ChartManagerError

    inner = typer.Typer()
    helmrelease_cli.register(inner)

    def _wrap(fn):  # type: ignore[no-untyped-def]
        import functools
        import sys as _sys

        @functools.wraps(fn)
        def wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
            try:
                return fn(*args, **kwargs)
            except ChartManagerError as exc:
                print(f"error: {exc}", file=_sys.stderr)
                raise typer.Exit(code=1) from exc
            except FileNotFoundError as exc:
                print(
                    f"error: required binary not found: {exc.filename or exc}",
                    file=_sys.stderr,
                )
                raise typer.Exit(code=127) from exc

        return wrapped

    app = typer.Typer()
    for cmd in inner.registered_commands:
        app.command(cmd.name)(_wrap(cmd.callback))
    return app


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
class _FakeMonitorService:
    captured_requests: list[Any] = field(default_factory=list)
    captured_progress: list[Any] = field(default_factory=list)
    result: MonitorResult | None = None
    raise_exc: BaseException | None = None

    def monitor(self, request: Any) -> MonitorResult:
        self.captured_requests.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.result is not None
        return self.result


@dataclass
class _FakeTestService:
    captured_requests: list[Any] = field(default_factory=list)
    captured_progress: list[Any] = field(default_factory=list)
    result: TestResult | None = None
    raise_exc: BaseException | None = None

    def test(self, request: Any) -> TestResult:
        self.captured_requests.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.result is not None
        return self.result


def _install_fake_monitor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: MonitorResult,
    raise_exc: BaseException | None = None,
) -> _FakeMonitorService:
    fake = _FakeMonitorService(result=result, raise_exc=raise_exc)
    factory_calls: list[dict[str, Any]] = []

    def _factory(*, progress: Any) -> _FakeMonitorService:
        factory_calls.append({"progress": progress})
        fake.captured_progress.append(progress)
        return fake

    monkeypatch.setattr(helmrelease_cli, "_make_monitor_service", _factory)
    fake.factory_calls = factory_calls  # type: ignore[attr-defined]
    return fake


def _install_fake_test(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: TestResult,
    raise_exc: BaseException | None = None,
) -> _FakeTestService:
    fake = _FakeTestService(result=result, raise_exc=raise_exc)
    factory_calls: list[dict[str, Any]] = []

    def _factory(*, progress: Any) -> _FakeTestService:
        factory_calls.append({"progress": progress})
        fake.captured_progress.append(progress)
        return fake

    monkeypatch.setattr(helmrelease_cli, "_make_test_service", _factory)
    fake.factory_calls = factory_calls  # type: ignore[attr-defined]
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


@pytest.fixture
def runner() -> CliRunner:
    # Click 8.2+/typer 0.26 separate stderr by default; the mix_stderr kwarg
    # was removed. res.stderr is always isolated.
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)


_BASE = ["monitor", "--chart", "loki", "--version", "0.2.0"]


# ----- required option tests ----------------------------------------------


def test_chart_option_required(runner: CliRunner) -> None:
    res = runner.invoke(_build_app(), ["monitor", "--version", "0.2.0"])
    assert res.exit_code == 2


def test_version_option_required(runner: CliRunner) -> None:
    res = runner.invoke(_build_app(), ["monitor", "--chart", "loki"])
    assert res.exit_code == 2


# ----- pretty / json modes -------------------------------------------------


def test_pretty_ok_exit_0_summary_in_stdout(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_monitor(monkeypatch, result=_ok_result())
    res = runner.invoke(_build_app(), [*_BASE, "--output", "pretty"])
    assert res.exit_code == 0
    # diagnostics never written for ready outcomes
    assert "InstallFailed" not in res.stdout
    assert "ready" in res.stdout


def test_pretty_failure_exit_1_diagnostics_in_stdout(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_monitor(monkeypatch, result=_bad_result())
    res = runner.invoke(_build_app(), [*_BASE, "--output", "pretty"])
    assert res.exit_code == 1
    assert "InstallFailed" in res.stdout


def test_json_mode_emits_parseable_payload(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_monitor(monkeypatch, result=_ok_result())
    res = runner.invoke(_build_app(), [*_BASE, "--output", "json"])
    assert res.exit_code == 0
    assert res.stdout.endswith("\n")
    payload = json.loads(res.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "monitor"
    assert payload["ok"] is True
    # No ANSI escapes leaked into json stream.
    assert "\x1b[" not in res.stdout


def test_json_payload_round_trips_with_failure(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_monitor(monkeypatch, result=_bad_result())
    res = runner.invoke(_build_app(), [*_BASE, "--output", "json"])
    assert res.exit_code == 1
    payload = json.loads(res.stdout)
    assert payload["ok"] is False
    assert payload["outcomes"][0]["verdict"] == "failed"
    assert payload["outcomes"][0]["diagnostics"]


# ----- progress wiring -----------------------------------------------------


def test_pretty_wires_progress_callback(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_monitor(monkeypatch, result=_ok_result())
    res = runner.invoke(_build_app(), [*_BASE, "--output", "pretty"])
    assert res.exit_code == 0
    assert fake.captured_progress[0] is not None


def test_json_mode_omits_progress_callback(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_monitor(monkeypatch, result=_ok_result())
    res = runner.invoke(_build_app(), [*_BASE, "--output", "json"])
    assert res.exit_code == 0
    assert fake.captured_progress[0] is None


# ----- auto mode resolution ------------------------------------------------


def test_auto_mode_under_ci_env_picks_json(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    _install_fake_monitor(monkeypatch, result=_ok_result())
    res = runner.invoke(_build_app(), [*_BASE, "--output", "auto"])
    assert res.exit_code == 0
    assert res.stdout.endswith("\n")
    payload = json.loads(res.stdout)
    assert payload["schema_version"] == 1


def test_pretty_explicit_under_non_tty_still_pretty(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_monitor(monkeypatch, result=_ok_result())
    res = runner.invoke(_build_app(), [*_BASE, "--output", "pretty"])
    # CliRunner is non-tty; explicit pretty must not be coerced to json.
    assert res.exit_code == 0
    # No JSON schema marker in stdout.
    assert "schema_version" not in res.stdout


# ----- namespace coercion --------------------------------------------------


def test_namespace_empty_string_coerced_to_none(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_monitor(monkeypatch, result=_ok_result())
    res = runner.invoke(_build_app(), [*_BASE, "--namespace", ""])
    assert res.exit_code == 0
    assert fake.captured_requests[0].namespace is None


def test_namespace_value_plumbed(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_monitor(monkeypatch, result=_ok_result())
    res = runner.invoke(_build_app(), [*_BASE, "--namespace", "obs"])
    assert res.exit_code == 0
    assert fake.captured_requests[0].namespace == "obs"


# ----- error handling ------------------------------------------------------


def test_chart_manager_error_maps_to_exit_1(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chart_manager.plumbing.errors import ChartManagerError

    _install_fake_monitor(
        monkeypatch,
        result=_ok_result(),
        raise_exc=ChartManagerError("apiserver unreachable"),
    )
    res = runner.invoke(_build_app(), _BASE)
    assert res.exit_code == 1
    assert "apiserver unreachable" in res.stderr


def test_file_not_found_maps_to_exit_127(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_monitor(
        monkeypatch,
        result=_ok_result(),
        raise_exc=FileNotFoundError(2, "No such file or directory", "kubectl"),
    )
    res = runner.invoke(_build_app(), _BASE)
    assert res.exit_code == 127


# ----- json schema golden --------------------------------------------------


def test_json_schema_matches_expected_dict(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    res = runner.invoke(_build_app(), [*_BASE, "--output", "json"])
    assert res.exit_code == 1
    payload = json.loads(res.stdout)
    verdicts = [o["verdict"] for o in payload["outcomes"]]
    assert verdicts == ["ready", "failed", "timed-out"]
    assert payload["outcomes"][2]["reason"] == "PerHRBudgetExhausted"
    assert payload["ok"] is False


# ----- test (helm test) command -------------------------------------------


def test_test_pod_log_tail_plumbed(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    result = TestResult(
        outcomes=(_passed_test_outcome(_ref()),),
        total_duration_seconds=2.0,
        total_timed_out=False,
    )
    fake = _install_fake_test(monkeypatch, result=result)
    res = runner.invoke(
        _build_app(),
        ["test", "--chart", "loki", "--version", "0.2.0", "--pod-log-tail", "50"],
    )
    assert res.exit_code == 0
    assert fake.captured_requests[0].pod_log_tail == 50


def test_monitor_fail_fast_plumbed(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_monitor(monkeypatch, result=_ok_result())
    res = runner.invoke(_build_app(), [*_BASE, "--fail-fast"])
    assert res.exit_code == 0
    assert fake.captured_requests[0].fail_fast is True


# ----- progress driver thread safety smoke --------------------------------


def test_pretty_progress_driver_thread_safety() -> None:
    import threading as _threading
    from datetime import UTC, datetime

    from rich.console import Console as _Console

    driver = _PrettyProgressDriver(_Console(quiet=True))
    errors: list[BaseException] = []

    def fire(i: int) -> None:
        try:
            for j in range(20):
                driver(
                    _ref(f"hr{i}", "ns"),
                    Transition(at=datetime.now(UTC), phase=f"p{j}", detail=f"d{j}"),
                )
        except BaseException as exc:  # pragma: no cover -- thread safety smoke
            errors.append(exc)

    threads = [_threading.Thread(target=fire, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


# ----- promote relocation smoke -------------------------------------------


def test_promote_command_registered() -> None:
    app = _build_app()
    # Inspect typer's registered_commands to confirm promote moved cleanly.
    names = {cmd.name for cmd in app.registered_commands}
    assert "promote" in names
    assert "monitor" in names
    assert "test" in names


# ----- no-match outcome rendering -----------------------------------------


def test_no_match_outcome_pretty_message(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    res = runner.invoke(_build_app(), [*_BASE, "--output", "pretty"])
    assert res.exit_code == 1
    assert "no helmreleases matched" in res.stdout


# ----- wire-contract byte goldens ------------------------------------------
#
# These lock the exact bytes emitted by `helmrelease monitor|test --output
# json`. They are the acceptance criterion for any refactor that moves the
# serializers: the payload is a versioned wire contract, so a diff here is a
# breaking change for every downstream consumer, not a cosmetic churn.
#
# Regenerate with:
#   REGEN_GOLDEN=1 uv run pytest tests/test_cli_helmrelease.py


_FIXED_TS = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


def _golden_status(ref: HelmReleaseRef) -> HelmReleaseStatus:
    """A fully-populated status with fixed timestamps (deterministic bytes)."""
    return HelmReleaseStatus(
        ref=ref,
        observed_at=_FIXED_TS,
        generation=4,
        observed_generation=4,
        resource_version="9182",
        suspended=False,
        desired_chart_name="loki",
        desired_chart_version="0.2.0",
        last_applied_revision="0.2.0",
        history_chart_version="0.2.0",
        conditions=(
            ConditionSnapshot(
                type="Ready",
                status="True",
                reason="ReconciliationSucceeded",
                message="Helm upgrade succeeded for release loki/loki.v4",
                last_transition_time=_FIXED_TS,
            ),
            ConditionSnapshot(
                type="Released",
                status="True",
                reason="UpgradeSucceeded",
                message="Helm upgrade succeeded",
                last_transition_time=None,
            ),
        ),
    )


def _golden_monitor_result() -> MonitorResult:
    """Exercise every branch of the monitor payload: workloads, transitions, no-status."""
    ref = _ref()
    rollout = WorkloadRollout(
        workload=OwnedWorkload(
            kind="Deployment",
            namespace="loki",
            name="loki-gateway",
            desired=2,
            ready=2,
            available=2,
        ),
        converged=True,
        generation=4,
        observed_generation=4,
    )
    ready = MonitorOutcome(
        ref=ref,
        verdict="ready",
        reason="ReconciliationSucceeded",
        last_status=_golden_status(ref),
        last_workloads=(rollout,),
        recent_transitions=(
            Transition(at=_FIXED_TS, phase="reconciling", detail="upgrade in progress"),
            Transition(at=_FIXED_TS, phase="ready", detail="all workloads converged"),
        ),
        diagnostics=None,
        duration_seconds=12.25,
    )
    # last_status=None exercises the `if status else` fallbacks.
    timed_out = MonitorOutcome(
        ref=_ref("mimir", "mimir"),
        verdict="timed-out",
        reason="PerHRBudgetExhausted",
        last_status=None,
        last_workloads=(),
        recent_transitions=(),
        diagnostics="## mimir/mimir - timed-out: PerHRBudgetExhausted",
        duration_seconds=300.0,
    )
    return MonitorResult(
        outcomes=(ready, timed_out),
        total_duration_seconds=312.5,
        total_timed_out=True,
    )


def _golden_test_result() -> TestResult:
    """Exercise every branch of the test payload: pods, phase log, diagnostics."""
    ref = _ref()
    failed = TestOutcome(
        ref=ref,
        verdict="failed",
        reason="TestPodFailed",
        helm_test_returncode=1,
        helm_test_stdout="Phase: Failed",
        helm_test_stderr="error: pod loki-test failed",
        test_pods=(
            TestPodSnapshot(
                namespace="loki",
                name="loki-test",
                phase="Failed",
                logs="connection refused",
                previous_logs=None,
            ),
            TestPodSnapshot(
                namespace="loki",
                name="loki-test-retry",
                phase="Succeeded",
                logs="ok",
                previous_logs="connection refused",
            ),
        ),
        last_status=_golden_status(ref),
        phase_log=(
            Transition(at=_FIXED_TS, phase="running", detail="helm test started"),
            Transition(at=_FIXED_TS, phase="failed", detail="pod loki-test failed"),
        ),
        diagnostics="## loki/loki - failed: TestPodFailed\nconnection refused",
        duration_seconds=7.5,
    )
    return TestResult(
        outcomes=(failed, _passed_test_outcome(_ref("mimir", "mimir"))),
        total_duration_seconds=9.5,
        total_timed_out=False,
    )


def _assert_golden(actual: str, path: Path) -> None:
    if os.environ.get("REGEN_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        pytest.fail(f"regenerated golden at {path}")
    assert actual == path.read_text()


def test_monitor_json_bytes_match_golden() -> None:
    buf = io.StringIO()
    render_monitor_json(_golden_monitor_result(), buf, chart="loki", version="0.2.0")
    _assert_golden(buf.getvalue(), MONITOR_JSON_GOLDEN)


def test_test_json_bytes_match_golden() -> None:
    result = _golden_test_result()
    # `_passed_test_outcome` embeds a now() timestamp in last_status, but
    # last_status is not part of the test payload — pin it out of the way so
    # the golden stays deterministic.
    buf = io.StringIO()
    render_test_json(result, buf, chart="loki", version="0.2.0")
    _assert_golden(buf.getvalue(), TEST_JSON_GOLDEN)


def test_wire_module_does_not_import_rich() -> None:
    """A non-terminal surface must be able to import the payload projections.

    Subprocess because this test module already imports rich.
    """
    import subprocess
    import sys as _sys

    probe = (
        "import sys; import chart_manager.services.helmrelease.wire as w; "
        "assert w.monitor_to_dict and w.test_to_dict and w.SCHEMA_VERSION == 1; "
        "print(','.join(sorted(m for m in sys.modules "
        "if m == 'rich' or m.startswith('rich.'))))"
    )
    proc = subprocess.run(
        [_sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert proc.stdout.strip() == "", f"wire module leaked rich: {proc.stdout.strip()}"


def test_monitor_json_is_single_line_compact_and_flushed() -> None:
    """Encoder options are part of the contract: compact, sorted, one trailing \\n."""
    buf = io.StringIO()
    render_monitor_json(_golden_monitor_result(), buf, chart="loki", version="0.2.0")
    raw = buf.getvalue()
    assert raw.endswith("\n")
    assert raw.count("\n") == 1
    assert ", " not in raw.split('"diagnostics"')[0]  # separators=(",", ":")
    body = raw[:-1]
    keys = list(json.loads(body).keys())
    assert keys == sorted(keys)  # sort_keys=True
