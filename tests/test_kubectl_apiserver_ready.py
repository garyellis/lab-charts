"""Coverage for `Kubectl.wait_apiserver_ready`.

Gates the install path after `Kind.ensure_cluster` returns from the
start-stopped branch: docker reports the containers up but kube-apiserver
takes seconds to bind /readyz. Without this gate `helm list -A` races.
"""
from __future__ import annotations

import pytest

from chart_manager.integrations import kubectl as kubectl_module
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from tests.conftest import FakeCommandRunner, Reply


def _polls(*replies: Reply) -> FakeCommandRunner:
    """A readiness-probe runner whose last reply repeats.

    The loop keeps polling until the clock or an "ok" stops it, so the
    script says what the interesting polls return and nothing about how
    many times the final state is observed.
    """
    return FakeCommandRunner(when_exhausted="repeat").script(*replies)


def _unavailable(stderr: str = "Service Unavailable") -> Reply:
    """One failed readiness poll; Kubectl aggregates stderr into the timeout."""
    return Reply(returncode=1, stderr=stderr)

def test_wait_apiserver_ready_succeeds_on_first_ok() -> None:
    runner = _polls(Reply(stdout="ok"))

    Kubectl(runner=runner).wait_apiserver_ready()

    assert runner.calls == [("kubectl", "get", "--raw=/readyz")]


def test_wait_apiserver_ready_polls_until_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # Skip the real sleep -- we just want to verify the polling loop
    # actually retries on non-zero rc rather than failing immediately.
    monkeypatch.setattr(kubectl_module.time, "sleep", lambda _s: None)

    runner = _polls(_unavailable(), _unavailable(), Reply(stdout="ok"))

    Kubectl(runner=runner).wait_apiserver_ready(poll_interval=0.0)

    assert len(runner.calls) == 3
    assert all(c == ("kubectl", "get", "--raw=/readyz") for c in runner.calls)


def test_wait_apiserver_ready_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kubectl_module.time, "sleep", lambda _s: None)

    # Advance monotonic enough that the deadline passes after one poll.
    clock = iter([0.0, 0.0, 100.0, 100.0])
    monkeypatch.setattr(kubectl_module.time, "monotonic", lambda: next(clock))

    runner = _polls(_unavailable())

    with pytest.raises(ExternalCommandError) as excinfo:
        Kubectl(runner=runner).wait_apiserver_ready(timeout="60s")

    msg = str(excinfo.value)
    assert "did not become ready within 60s" in msg
    # A failed poll emits "Service Unavailable" on stderr; the
    # aggregated-stderr branch must surface it in the timeout message.
    assert "Service Unavailable" in msg



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

    runner = _polls(
        _unavailable("dial tcp: lookup kubernetes: no such host"),
        _unavailable("503 Service Unavailable"),
        _unavailable("dial tcp: lookup kubernetes: no such host"),
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
        Kubectl(runner=_polls(Reply(stdout="ok"))).wait_apiserver_ready(
            timeout="not-a-duration"
        )
    assert "invalid duration" in str(excinfo.value)
    assert "not-a-duration" in str(excinfo.value)
