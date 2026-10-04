"""`chart publish` at the CLI: flags, output and exit codes, with `run()` faked."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.commands import publish
from chart_manager.commands.publish import cli as publish_cli
from tests.conftest import cli, write_workspace

GRAFANA = publish.PublishedChart(
    "grafana", "1.2.3-pr.4", "oci://harbor/library/grafana:1.2.3-pr.4", "sha256:abc"
)


class FakeRun:
    """Stands in for `publish.run`: records each request and answers with `result`."""

    def __init__(self) -> None:
        self.requests: list[publish.PublishRequest] = []
        self.result = publish.PublishOutcome((GRAFANA,), publish.PublishKind.PREVIEW)

    def __call__(self, request: publish.PublishRequest, **_: object) -> publish.PublishOutcome:
        self.requests.append(request)
        return self.result


@pytest.fixture
def fake_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    write_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    fake = FakeRun()
    monkeypatch.setattr(publish_cli, "run", fake)
    return fake


def test_flags_become_the_request(fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHART_MANAGER_OCI_REPOSITORY", "oci://harbor/library")
    monkeypatch.setenv("CHART_MANAGER_OCI_CA_FILE", "/tmp/lab-ca.crt")

    result = cli(
        "chart", "publish", "grafana", "loki", "grafana",
        "--version-suffix", "pr.4",
        "--kind", "preview",
        "--build-correlation-id", "owner/repository#9",
        "--pr-url", "https://github.test/owner/repository/pull/9",
        "--git-sha", "abcdef12",
        "--operation-id", "200.1",
    )  # fmt: skip

    assert result.exit_code == 0
    assert fake_run.requests == [
        publish.PublishRequest(
            charts=("grafana", "loki", "grafana"),
            repository="oci://harbor/library",
            version_suffix="pr.4",
            ca_file=Path("/tmp/lab-ca.crt"),
            kind=publish.PublishKind.PREVIEW,
            build_correlation_id="owner/repository#9",
            pr_url="https://github.test/owner/repository/pull/9",
            git_sha="abcdef12",
            operation_id="200.1",
        )
    ]


def test_a_missing_repository_is_a_usage_error(fake_run: FakeRun) -> None:
    result = cli("chart", "publish", "grafana")

    assert result.exit_code == 2
    assert fake_run.requests == []


FAILED_PUSH = publish.PublishedChart("loki", "2.0.0", error="registry rejected upload")
FAILED_EVENT = publish.PublishTelemetryFailure("grafana", "1.2.3-pr.4", "cosmos unavailable")


@pytest.mark.parametrize(
    ("charts", "failures", "strict", "exit_code", "line"),
    [
        (
            (GRAFANA,),
            (),
            [],
            0,
            "published grafana oci://harbor/library/grafana:1.2.3-pr.4 (sha256:abc)",
        ),
        ((GRAFANA, FAILED_PUSH), (), [], 1, "failed loki: registry rejected upload"),
        ((GRAFANA,), (FAILED_EVENT,), [], 0, "event failed grafana 1.2.3-pr.4: cosmos unavailable"),
        ((GRAFANA,), (FAILED_EVENT,), ["--strict-events"], 1, "event failed grafana"),
    ],
    ids=["pushed", "push-failed", "event-failed", "event-failed-strict"],
)
def test_each_row_is_a_stderr_line_and_a_failure_exits_1(
    fake_run: FakeRun,
    charts: tuple[publish.PublishedChart, ...],
    failures: tuple[publish.PublishTelemetryFailure, ...],
    strict: list[str],
    exit_code: int,
    line: str,
) -> None:
    fake_run.result = publish.PublishOutcome(charts, publish.PublishKind.PREVIEW, failures)

    result = cli("chart", "publish", "grafana", "--repository", "oci://harbor/library", *strict)

    assert (result.exit_code, result.stdout) == (exit_code, "")
    assert line in result.stderr
    assert fake_run.requests[0].dry_run is False


def test_a_dry_run_prints_the_plan_on_stdout(fake_run: FakeRun) -> None:
    planned = publish.PublishedChart("grafana", "1.2.3-pr.4", GRAFANA.reference)
    fake_run.result = publish.PublishOutcome((planned,), publish.PublishKind.PREVIEW)

    result = cli("chart", "publish", "grafana", "--repository", "oci://h/l", "--dry-run")

    assert result.exit_code == 0
    assert fake_run.requests[0].dry_run is True
    assert result.stdout == (
        "would publish grafana 1.2.3-pr.4 -> oci://harbor/library/grafana:1.2.3-pr.4 (preview)\n"
    )
    assert "dry run: packaged 1 chart(s); pushed nothing" in result.stderr
