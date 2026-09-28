"""The Helm status adapter preserves the exit code used by release preflights."""

from chart_manager.integrations.helm import Helm

from .conftest import FakeCommandRunner


def test_status_returns_nonzero_result_without_raising() -> None:
    runner = FakeCommandRunner().respond(
        ("helm", "status", "shared", "--namespace", "monitoring"),
        returncode=1,
        stderr="Error: release: shared: not found",
    )

    result = Helm(runner=runner).status("shared", namespace="monitoring")

    assert result.returncode == 1
    assert result.stderr == "Error: release: shared: not found"
    assert runner.records[0].check is False
