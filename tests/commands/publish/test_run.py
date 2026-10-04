"""`publish.run()`: prepare every chart, then push each, one row per chart."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from chart_manager.commands.publish import (
    PublishKind,
    PublishOutcome,
    PublishRequest,
    PublishTelemetryFailure,
)
from chart_manager.commands.publish.run import run
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError, SpecError
from chart_manager.services.events.lifecycle import BuildPhase, PlatformLifecycleEvent
from chart_manager.services.events.store import EventQuery
from chart_manager.services.events.writer import EventWriter
from chart_manager.shared.settings import Settings
from tests.conftest import FakeCommandRunner, MakeChart, plain_argv, workspace_for

REPOSITORY = "oci://registry.local/library"


class _Store:
    """An event store that records each event and fails for `fail_chart`."""

    def __init__(self, *, fail_chart: str | None = None) -> None:
        self.events: list[PlatformLifecycleEvent] = []
        self.fail_chart = fail_chart

    def write(self, event: PlatformLifecycleEvent) -> None:
        self.events.append(event)
        if event.chart_name == self.fail_chart:
            raise RuntimeError("events backend unavailable")

    def query(self, query: EventQuery) -> list[dict[str, object]]:
        raise NotImplementedError


def _helm(*charts: str, fail: tuple[str, str] | None = None) -> FakeCommandRunner:
    """Helm that packages each chart as `<chart>.tgz`; `fail` is a (verb, chart) that exits 1."""
    runner = FakeCommandRunner()
    if fail is not None:
        runner.respond(lambda argv: _step(argv) == fail, returncode=1, stderr="boom")
    for chart in charts:
        runner.respond(
            lambda argv, chart=chart: _step(argv) == ("package", chart),
            stdout=f"Successfully packaged chart and saved it to: {chart}.tgz",
        )
    runner.respond(lambda argv: plain_argv(argv)[1] == "push", stdout="Digest: sha256:abc")
    return runner


def _step(argv: tuple[str, ...]) -> tuple[str, str]:
    """(helm verb, chart) for a recorded helm call."""
    argv = plain_argv(argv)
    return argv[1], Path(argv[3] if argv[1] == "dependency" else argv[2]).name.split(".")[0]


def _run(
    root: Path, request: PublishRequest, runner: FakeCommandRunner, store: _Store | None = None
) -> PublishOutcome:
    return run(
        request,
        workspace=workspace_for(root),
        runner=runner,
        settings=Settings(kube_context="lab"),
        events=EventWriter(store or _Store()),
    )


def test_every_chart_is_prepared_before_any_push(chart_root: Path, make_chart: MakeChart) -> None:
    make_chart("alpha", version="1.0.0")
    make_chart("beta", version="2.0.0")
    runner = _helm("alpha", "beta")

    outcome = _run(
        chart_root,
        PublishRequest(
            ("alpha", "beta"), REPOSITORY, version_suffix="pr.8", ca_file=Path("ca.crt")
        ),
        runner,
    )

    assert [_step(argv) for argv in runner.calls] == [
        ("dependency", "alpha"),
        ("package", "alpha"),
        ("dependency", "beta"),
        ("package", "beta"),
        ("push", "alpha"),
        ("push", "beta"),
    ]
    assert runner.calls[0][-2:] == ("--kube-context", "lab")
    assert runner.calls[1][-2:] == ("--version", "1.0.0-pr.8")
    assert plain_argv(runner.calls[4])[3:] == (REPOSITORY, "--ca-file", "ca.crt")
    assert [(row.chart, row.version, row.reference, row.digest) for row in outcome.charts] == [
        ("alpha", "1.0.0-pr.8", f"{REPOSITORY}/alpha:1.0.0-pr.8", "sha256:abc"),
        ("beta", "2.0.0-pr.8", f"{REPOSITORY}/beta:2.0.0-pr.8", "sha256:abc"),
    ]
    assert outcome.ok


@pytest.mark.parametrize(
    ("charts", "fail", "error"),
    [
        (("alpha", "beta"), ("package", "beta"), ExternalCommandError),
        (("alpha", "beta"), ("dependency", "beta"), ExternalCommandError),
        (("alpha", "missing"), None, ChartManagerError),
    ],
    ids=["package", "dependency-update", "unknown-chart"],
)
def test_a_preparation_failure_pushes_nothing(
    chart_root: Path,
    make_chart: MakeChart,
    charts: tuple[str, ...],
    fail: tuple[str, str] | None,
    error: type[Exception],
) -> None:
    make_chart("alpha")
    make_chart("beta")
    runner, store = _helm("alpha", "beta", fail=fail), _Store()

    with pytest.raises(error):
        _run(chart_root, PublishRequest(charts, REPOSITORY), runner, store)

    assert "push" not in [_step(argv)[0] for argv in runner.calls]
    assert store.events == []


def test_push_failures_are_consolidated_and_the_other_pushes_continue(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha")
    make_chart("beta")
    runner, store = _helm("alpha", "beta", fail=("push", "alpha")), _Store()

    outcome = _run(chart_root, PublishRequest(("alpha", "beta"), REPOSITORY), runner, store)

    assert [_step(argv) for argv in runner.calls][-2:] == [("push", "alpha"), ("push", "beta")]
    assert [(row.chart, row.ok, row.reference) for row in outcome.charts] == [
        ("alpha", False, None),
        ("beta", True, f"{REPOSITORY}/beta:0.1.0"),
    ]
    assert not outcome.ok
    assert [event.chart_name for event in store.events] == ["beta"]


@pytest.mark.parametrize(
    ("suffix", "phase"),
    [("pr.8", BuildPhase.PREVIEW_PUBLISHED), (None, BuildPhase.PUBLISHED)],
    ids=["preview", "release"],
)
def test_each_push_emits_a_retry_safe_build_event(
    chart_root: Path, make_chart: MakeChart, suffix: str | None, phase: BuildPhase
) -> None:
    make_chart("alpha", version="1.0.0")
    make_chart("beta", version="2.0.0")
    store = _Store()
    request = PublishRequest(
        ("alpha", "beta"),
        REPOSITORY,
        version_suffix=suffix,
        build_correlation_id="owner/repository#8",
        pr_url="https://github.test/owner/repository/pull/8",
        git_sha="abcdef12",
        operation_id="100.1",
    )

    first = _run(chart_root, request, _helm("alpha", "beta"), store)
    _run(chart_root, request, _helm("alpha", "beta"), store)

    assert first.telemetry_ok
    version = "1.0.0-pr.8" if suffix else "1.0.0"
    alpha = store.events[0]
    assert [event.build_phase for event in store.events] == [phase] * 4
    assert (alpha.chart_name, alpha.chart_version) == ("alpha", version)
    assert (alpha.build_correlation_id, alpha.pr_url, alpha.git_sha) == (
        "owner/repository#8",
        "https://github.test/owner/repository/pull/8",
        "abcdef12",
    )
    assert alpha.detail == {
        "publish_kind": "preview" if suffix else "release",
        "repository": REPOSITORY,
        "reference": f"{REPOSITORY}/alpha:{version}",
        "digest": "sha256:abc",
        "operation_id": "100.1",
        "batch_index": 1,
        "batch_count": 2,
    }
    assert alpha.idempotency_key == store.events[2].idempotency_key
    assert alpha.idempotency_key != store.events[1].idempotency_key


def test_an_event_failure_is_reported_without_failing_the_push(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha")
    make_chart("beta")
    store = _Store(fail_chart="alpha")

    outcome = _run(
        chart_root, PublishRequest(("alpha", "beta"), REPOSITORY), _helm("alpha", "beta"), store
    )

    assert outcome.ok
    assert len(store.events) == 2
    assert outcome.telemetry_failures == (
        PublishTelemetryFailure("alpha", "0.1.0", "events backend unavailable"),
    )


def test_a_dry_run_prepares_like_a_real_publish_then_pushes_nothing_and_emits_no_event(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha", version="1.0.0")
    make_chart("beta", version="2.0.0")
    planning, real, store = _helm("alpha", "beta"), _helm("alpha", "beta"), _Store()
    request = PublishRequest(("alpha", "beta"), f"{REPOSITORY}/", version_suffix="pr.8")

    planned = _run(chart_root, replace(request, dry_run=True), planning, store)
    _run(chart_root, request, real, _Store())

    assert [_step(argv) for argv in real.calls] == [
        *(_step(argv) for argv in planning.calls),
        ("push", "alpha"),
        ("push", "beta"),
    ]
    assert (planned.kind, store.events) == (PublishKind.PREVIEW, [])
    assert [(row.chart, row.version, row.reference, row.digest) for row in planned.charts] == [
        ("alpha", "1.0.0-pr.8", f"{REPOSITORY}/alpha:1.0.0-pr.8", None),
        ("beta", "2.0.0-pr.8", f"{REPOSITORY}/beta:2.0.0-pr.8", None),
    ]


@pytest.mark.parametrize(
    ("chart_version", "request_", "version", "kind"),
    [
        ("1.2.3", {}, "1.2.3", PublishKind.RELEASE),
        ("1.2.3", {"version": "1.0.0+001"}, "1.0.0+001", PublishKind.RELEASE),
        ("6.2.1", {"version_suffix": "pr.318"}, "6.2.1-pr.318", PublishKind.PREVIEW),
        (
            "6.2.1-rc.1+build.7",
            {"version_suffix": "pr.318"},
            "6.2.1-rc.1.pr.318+build.7",
            PublishKind.PREVIEW,
        ),
    ],
    ids=["chart-version", "exact-version", "suffix", "suffix-keeps-prerelease-and-build"],
)
def test_the_published_version_and_kind(
    chart_root: Path,
    make_chart: MakeChart,
    chart_version: str,
    request_: dict[str, str],
    version: str,
    kind: PublishKind,
) -> None:
    make_chart("alpha", version=chart_version)

    outcome = _run(
        chart_root, PublishRequest(("alpha",), REPOSITORY, dry_run=True, **request_), _helm("alpha")
    )

    assert (outcome.charts[0].version, outcome.kind) == (version, kind)


@pytest.mark.parametrize(
    ("charts", "request_", "message"),
    [
        ((), {}, "at least one chart"),
        (("alpha",), {"repository": "https://registry.local"}, "oci://"),
        (("alpha",), {"version": "1.0.0", "version_suffix": "pr.1"}, "mutually exclusive"),
        (("alpha", "beta"), {"version": "2.0.0"}, "exactly one"),
        (("alpha",), {"version_suffix": "pr.1", "kind": PublishKind.RELEASE}, "release publishing"),
        (("alpha",), {"version": "1.0.0-01"}, "invalid SemVer version"),
        (("alpha",), {"version_suffix": ""}, "suffix"),
        (("alpha",), {"version_suffix": "-pr.1"}, "suffix"),
        (("alpha",), {"version_suffix": "pr..1"}, "suffix"),
        (("alpha",), {"version_suffix": "pr.01"}, "suffix"),
        (("alpha",), {"version_suffix": "pr_1"}, "suffix"),
    ],
)
def test_an_invalid_request_is_rejected_before_any_helm_call(
    chart_root: Path,
    make_chart: MakeChart,
    charts: tuple[str, ...],
    request_: dict[str, object],
    message: str,
) -> None:
    make_chart("alpha")
    make_chart("beta")
    runner = _helm()
    request = replace(PublishRequest(charts, REPOSITORY, dry_run=True), **request_)

    with pytest.raises(SpecError, match=message):
        _run(chart_root, request, runner)

    assert runner.calls == []
