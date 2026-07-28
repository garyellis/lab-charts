"""Upgrade dependencies share the container's one subprocess seam."""

from __future__ import annotations

from pathlib import Path

import yaml

from chart_manager.composition import Container
from chart_manager.services.upgrader import FinalizeRequest, UpgradeRequest
from tests.conftest import FakeCommandRunner, Reply


class _Container(Container):
    def __init__(self, runner: FakeCommandRunner) -> None:
        super().__init__()
        self.runner = runner

    def command_runner(self) -> FakeCommandRunner:
        return self.runner


def _chart(root: Path) -> Path:
    chart = root / "charts" / "loki"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        yaml.safe_dump({"apiVersion": "v2", "name": "loki", "version": "1.2.3"}),
        encoding="utf-8",
    )
    return chart


def test_upgrade_service_uses_shared_runner_for_git_and_renovate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ci-token")
    chart = _chart(tmp_path)
    (tmp_path / "renovate-global.json").write_text("{}\n", encoding="utf-8")
    (chart / "renovate.json").write_text("{}\n", encoding="utf-8")
    runner = FakeCommandRunner().script(
        Reply(stdout="git@github.com:owner/repository.git\n"),
        Reply(stdout=""),
        Reply(stdout="renovate complete\n"),
    )

    result = (
        _Container(runner)
        .upgrade_service(tmp_path)
        .upgrade(UpgradeRequest(root=tmp_path, chart_path=chart, dry_run=True))
    )

    assert result.outcome == "dry_run"
    assert runner.calls[0] == ("git", "remote", "get-url", "origin")
    assert runner.calls[1][:3] == ("git", "status", "--porcelain=v1")
    assert runner.calls[2] == ("renovate", "owner/repository")
    assert runner.records[2].env is not None
    assert runner.records[2].env["RENOVATE_DRY_RUN"] == "full"
    assert runner.records[2].env["RENOVATE_TOKEN"] == "ci-token"
    assert runner.records[2].env["RENOVATE_ADDITIONAL_CONFIG_FILE"] == str(
        chart / "renovate.json"
    )


def test_finalizer_baseline_read_uses_shared_runner(tmp_path: Path) -> None:
    chart = _chart(tmp_path)
    runner = FakeCommandRunner(
        stdout=yaml.safe_dump({"apiVersion": "v2", "name": "loki", "version": "1.2.3"})
    )

    result = (
        _Container(runner)
        .upgrade_finalizer(tmp_path)
        .finalize(FinalizeRequest(repo_root=tmp_path, chart_path=chart))
    )

    assert result.changed is False
    assert runner.calls == [
        ("git", "show", "HEAD:charts/loki/Chart.yaml"),
    ]


def test_upgrade_service_gets_the_container_memoized_event_writer(tmp_path: Path) -> None:
    """PR_OPEN opens the build timeline whose later phases CI emits.

    It has to land on the same store as those, so the service must get the
    container's *memoized* writer rather than a fresh one -- a fresh writer
    means a fresh EVENTS_BACKEND read and a fresh SDK client per call.
    """
    _chart(tmp_path)
    (tmp_path / "renovate-global.json").write_text("{}\n", encoding="utf-8")
    container = _Container(
        FakeCommandRunner().script(Reply(stdout="git@github.com:owner/repository.git\n"))
    )

    service = container.upgrade_service(tmp_path)

    telemetry = service._telemetry
    assert telemetry is not None
    assert telemetry.writer is container.event_writer()
    # Operator path: a dropped event must never fail an upgrade.
    assert telemetry.strict is False
