"""Top-level batch publish CLI surface."""

from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from chart_manager.cli import publish
from chart_manager.services.publish import PublishedChart, PublishResult


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
    assert "sha256:abc" in result.stdout
    assert calls == [
        (
            ["grafana", "loki"],
            {
                "repository": "oci://harbor/library",
                "version_suffix": "pr.4.gabc",
                "version": None,
                "ca_file": publish.Path("/tmp/lab-ca.crt"),
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
    assert "registry rejected upload" in result.stdout
