"""Coverage for `Kubectl.wait_apiserver_ready`.

Gates the install path after `Kind.ensure_cluster` returns from the
start-stopped branch: docker reports the containers up but kube-apiserver
takes seconds to bind /readyz. Without this gate `helm list -A` races.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from chart_manager.integrations import kubectl as kubectl_module
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.commands import CommandResult, CommandRunner
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError


class ScriptedRunner(CommandRunner):
    """Returns successive (returncode, stdout) tuples on each `run` call.

    The last entry repeats once exhausted -- so a poll that needs
    multiple "not ready" responses before "ok" can be expressed as
    `[(1, ""), (1, ""), (0, "ok")]`.
    """

    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._responses = responses
        self._index = 0

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
        idx = min(self._index, len(self._responses) - 1)
        self._index += 1
        returncode, stdout = self._responses[idx]
        return CommandResult(
            args=tuple(args),
            returncode=returncode,
            stdout=stdout,
            stderr="" if returncode == 0 else "Service Unavailable",
        )


def test_wait_apiserver_ready_succeeds_on_first_ok() -> None:
    runner = ScriptedRunner([(0, "ok")])

    Kubectl(runner=runner).wait_apiserver_ready()

    assert runner.calls == [("kubectl", "get", "--raw=/readyz")]


def test_wait_apiserver_ready_polls_until_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # Skip the real sleep -- we just want to verify the polling loop
    # actually retries on non-zero rc rather than failing immediately.
    monkeypatch.setattr(kubectl_module.time, "sleep", lambda _s: None)

    runner = ScriptedRunner([(1, ""), (1, ""), (0, "ok")])

    Kubectl(runner=runner).wait_apiserver_ready(poll_interval=0.0)

    assert len(runner.calls) == 3
    assert all(c == ("kubectl", "get", "--raw=/readyz") for c in runner.calls)


def test_wait_apiserver_ready_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kubectl_module.time, "sleep", lambda _s: None)

    # Advance monotonic enough that the deadline passes after one poll.
    clock = iter([0.0, 0.0, 100.0, 100.0])
    monkeypatch.setattr(kubectl_module.time, "monotonic", lambda: next(clock))

    runner = ScriptedRunner([(1, "")])

    with pytest.raises(ExternalCommandError) as excinfo:
        Kubectl(runner=runner).wait_apiserver_ready(timeout="60s")

    msg = str(excinfo.value)
    assert "did not become ready within 60s" in msg
    # ScriptedRunner emits "Service Unavailable" on non-zero rc; the
    # aggregated-stderr branch must surface it in the timeout message.
    assert "Service Unavailable" in msg


class _MultiStderrRunner(CommandRunner):
    """Returns scripted (returncode, stdout, stderr) triples per call."""

    def __init__(self, responses: list[tuple[int, str, str]]) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._responses = responses
        self._index = 0

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
        idx = min(self._index, len(self._responses) - 1)
        self._index += 1
        returncode, stdout, stderr = self._responses[idx]
        return CommandResult(
            args=tuple(args), returncode=returncode, stdout=stdout, stderr=stderr,
        )


def test_wait_apiserver_ready_aggregates_distinct_stderrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Different errors across polls (DNS, 503, repeat) must all appear in
    # the final timeout message -- with the repeat de-duped so the line
    # stays scannable.
    monkeypatch.setattr(kubectl_module.time, "sleep", lambda _s: None)
    # Five monotonic reads: deadline calc + four loop guards (three polls
    # under deadline, fourth trips it).
    clock = iter([0.0, 0.0, 1.0, 2.0, 100.0])
    monkeypatch.setattr(kubectl_module.time, "monotonic", lambda: next(clock))

    runner = _MultiStderrRunner(
        [
            (1, "", "dial tcp: lookup kubernetes: no such host"),
            (1, "", "503 Service Unavailable"),
            (1, "", "dial tcp: lookup kubernetes: no such host"),
        ]
    )

    with pytest.raises(ExternalCommandError) as excinfo:
        Kubectl(runner=runner).wait_apiserver_ready(timeout="60s")

    msg = str(excinfo.value)
    assert "no such host" in msg
    assert "503 Service Unavailable" in msg
    # De-duped: the repeated DNS error appears only once.
    assert msg.count("no such host") == 1


def test_wait_apiserver_ready_rejects_bad_timeout_literal() -> None:
    # A bad timeout literal must surface as ChartManagerError, not raw
    # ValueError, so the CLI's top-level handler reports it cleanly.
    with pytest.raises(ChartManagerError) as excinfo:
        Kubectl(runner=ScriptedRunner([(0, "ok")])).wait_apiserver_ready(
            timeout="not-a-duration"
        )
    assert "invalid duration" in str(excinfo.value)
    assert "not-a-duration" in str(excinfo.value)
