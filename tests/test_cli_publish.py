"""Top-level batch publish CLI surface."""

from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from chart_manager.cli import publish
from chart_manager.services.publish import (
    PublishedChart,
    PublishResult,
    PublishTelemetryFailure,
)


def _app() -> typer.Typer:
    app = typer.Typer()
    publish.register(app)
    app.command("other")(lambda: None)
    return app


def test_publish_passes_multiple_charts_in_one_service_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class Service:
        def publish(self, charts: list[str], **kwargs: object) -> PublishResult:
            calls.append((charts, kwargs))
            return PublishResult(
                (
                    PublishedChart(
                        "grafana",
                        "1.2.3-pr.4.gabc",
                        "oci://harbor/library/grafana:1.2.3-pr.4.gabc",
                        "sha256:abc",
                    ),
                )
            )

    monkeypatch.setattr(
        publish,
        "_container",
        lambda: SimpleNamespace(publish_service=lambda _root: Service()),
    )

    result = CliRunner().invoke(
        _app(),
        [
            "publish",
            "grafana",
            "loki",
            "--repository",
            "oci://harbor/library",
            "--version-suffix",
            "pr.4.gabc",
            "--ca-file",
            "/tmp/lab-ca.crt",
        ],
    )

    assert result.exit_code == 0
    assert "sha256:abc" in result.stderr
    assert calls == [
        (
            ["grafana", "loki"],
            {
                "repository": "oci://harbor/library",
                "version_suffix": "pr.4.gabc",
                "version": None,
                "ca_file": publish.Path("/tmp/lab-ca.crt"),
                "publish_kind": None,
                "build_correlation_id": None,
                "pr_url": None,
                "git_sha": None,
                "operation_id": None,
                "dry_run": False,
            },
        )
    ]


def test_publish_exits_nonzero_for_consolidated_push_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_value = PublishResult(
        (PublishedChart("grafana", "1.2.3", error="registry rejected upload"),)
    )
    monkeypatch.setattr(
        publish,
        "_container",
        lambda: SimpleNamespace(
            publish_service=lambda _root: SimpleNamespace(
                publish=lambda *_args, **_kwargs: result_value
            )
        ),
    )

    result = CliRunner().invoke(
        _app(),
        ["publish", "grafana", "--repository", "oci://harbor/library"],
    )

    assert result.exit_code == 1
    assert "registry rejected upload" in result.stderr


def test_publish_forwards_event_metadata_and_strict_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def run(*_args: object, **kwargs: object) -> PublishResult:
        calls.append(kwargs)
        return PublishResult(
            (PublishedChart("grafana", "1.2.3", "oci://registry/grafana:1.2.3"),),
            (PublishTelemetryFailure("grafana", "1.2.3", "cosmos unavailable"),),
        )

    monkeypatch.setattr(
        publish,
        "_container",
        lambda: SimpleNamespace(
            publish_service=lambda _root: SimpleNamespace(publish=run)
        ),
    )

    result = CliRunner().invoke(
        _app(),
        [
            "publish",
            "grafana",
            "--repository",
            "oci://registry",
            "--kind",
            "release",
            "--build-correlation-id",
            "owner/repository#9",
            "--pr-url",
            "https://github.test/owner/repository/pull/9",
            "--git-sha",
            "abcdef12",
            "--operation-id",
            "200.1",
            "--strict-events",
        ],
    )

    assert result.exit_code == 1
    assert "event failed" in result.stderr
    assert calls[0]["publish_kind"] == publish.PublishKind.RELEASE
    assert calls[0]["build_correlation_id"] == "owner/repository#9"
    assert calls[0]["pr_url"] == "https://github.test/owner/repository/pull/9"
    assert calls[0]["git_sha"] == "abcdef12"
    assert calls[0]["operation_id"] == "200.1"


def _plan_service(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, object]]
) -> None:
    """Stub the service so the surface is tested, not the plan computation."""

    def run(*_args: object, **kwargs: object) -> PublishResult:
        calls.append(kwargs)
        return PublishResult(
            (
                PublishedChart(
                    "grafana",
                    "1.2.3-pr.4.gabc",
                    "oci://harbor/library/grafana:1.2.3-pr.4.gabc",
                ),
            ),
            publish_kind=publish.PublishKind.PREVIEW,
            dry_run=True,
        )

    monkeypatch.setattr(
        publish,
        "_container",
        lambda: SimpleNamespace(publish_service=lambda _root: SimpleNamespace(publish=run)),
    )


def test_dry_run_is_forwarded_to_the_service_not_handled_in_the_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag is a service argument; the CLI must not fake the plan itself."""
    calls: list[dict[str, object]] = []
    _plan_service(monkeypatch, calls)

    result = CliRunner().invoke(
        _app(),
        ["publish", "grafana", "--repository", "oci://harbor/library", "--dry-run"],
    )

    assert result.exit_code == 0
    assert calls[0]["dry_run"] is True


def test_dry_run_plan_is_data_on_stdout_and_narration_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan is the selected projection, so `... --dry-run | ...` works."""
    _plan_service(monkeypatch, [])

    result = CliRunner().invoke(
        _app(),
        ["publish", "grafana", "--repository", "oci://harbor/library", "--dry-run"],
    )

    assert result.exit_code == 0
    assert "oci://harbor/library/grafana:1.2.3-pr.4.gabc" in result.stdout
    assert "preview" in result.stdout
    assert "dry run" not in result.stdout
    assert "dry run" in result.stderr
    assert "emitted no lifecycle event" in result.stderr


def test_publish_defaults_to_a_real_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting the flag must not silently plan instead of publishing."""
    calls: list[dict[str, object]] = []
    _plan_service(monkeypatch, calls)

    CliRunner().invoke(
        _app(), ["publish", "grafana", "--repository", "oci://harbor/library"]
    )

    assert calls[0]["dry_run"] is False
