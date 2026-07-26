"""CLI rendering of the results the lab/sandbox services now return.

`cli/main.py` is the only place that knows Rich exists. These tests pin
the output shape so the services-return-results refactor stayed a refactor:
the summary table, the access-hint blocks, the down/delete lines and the
progress narration must carry the same information they did when the
services printed them themselves.
"""
from __future__ import annotations

import pytest
import typer
from rich.console import Console

from chart_manager.cli import main as cli_main
from chart_manager.services.lab import (
    AccessHints,
    ClusterActionResult,
    EntryFailure,
    EntryOutcome,
    LabResult,
)
from chart_manager.services.progress import detail, failure, info, step, warn


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Console:
    """Swap the module console for a recording one and hand it back."""
    console = Console(record=True, width=200)
    monkeypatch.setattr(cli_main, "console", console)
    return console


# ----- progress event rendering ---------------------------------------------


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (step("Applying", "grafana:minimal -> observability"),
         "Applying grafana:minimal -> observability"),
        (step("Waiting for kube-apiserver"), "Waiting for kube-apiserver"),
        (detail("skip", "grafana:minimal (already installed in observability)"),
         "skip grafana:minimal (already installed in observability)"),
        (warn("could not list helm releases"), "warn: could not list helm releases"),
        (warn("cilium chart not found", label=None), "cilium chart not found"),
        (failure("apply failed:", "grafana:minimal -> boom"),
         "apply failed: grafana:minimal -> boom"),
        (info("pod/foo   0/1   CrashLoopBackOff"), "pod/foo   0/1   CrashLoopBackOff"),
    ],
)
def test_progress_events_render_label_then_message(
    captured: Console, event: object, expected: str
) -> None:
    cli_main._print_progress(event)  # type: ignore[arg-type]

    assert captured.export_text().strip() == expected


# ----- summary table --------------------------------------------------------


def test_lab_result_renders_every_bucket(captured: Console) -> None:
    result = LabResult(
        applied=(EntryOutcome("grafana", "minimal", "observability"),),
        no_change=(EntryOutcome("loki", "minimal", "observability"),),
        failed=(EntryFailure("mimir", "minimal", "observability", "boom"),),
    )

    cli_main._render_lab_result(result)
    out = captured.export_text()

    assert "Lab install summary" in out
    for token in ("applied", "grafana", "no-change", "loki", "failed", "mimir"):
        assert token in out
    assert "1 chart(s) failed" in out


def test_lab_result_omits_the_failure_line_when_ok(captured: Console) -> None:
    cli_main._render_lab_result(
        LabResult(applied=(EntryOutcome("grafana", "minimal", "observability"),))
    )

    assert "chart(s) failed" not in captured.export_text()


# ----- access hints ---------------------------------------------------------


def test_access_hints_render_urls_and_grafana_credentials(captured: Console) -> None:
    cli_main._render_access_hints(
        AccessHints(
            urls=("https://grafana.localhost/", "https://loki.localhost/"),
            grafana_url="https://grafana.localhost/",
            grafana_credentials=("admin", "s3cret"),
        )
    )
    out = captured.export_text()

    assert "URLs:" in out
    # Sort order is the service's; the renderer must not reshuffle it.
    assert out.index("grafana.localhost") < out.index("loki.localhost")
    assert "user: admin" in out
    assert "s3cret" in out


def test_access_hints_render_the_secret_read_failure_in_place(captured: Console) -> None:
    cli_main._render_access_hints(
        AccessHints(
            urls=("https://grafana.localhost/",),
            grafana_url="https://grafana.localhost/",
            grafana_error="secret not found",
        )
    )
    out = captured.export_text()

    assert "could not read admin password" in out
    assert "secret not found" in out
    assert "user: admin" not in out


def test_access_hints_render_the_virtualservice_listing_failure(captured: Console) -> None:
    cli_main._render_access_hints(
        AccessHints(urls_error="could not list VirtualServices (boom); skipping URL hints")
    )
    out = captured.export_text()

    assert "warn:" in out
    assert "could not list VirtualServices" in out
    assert "URLs:" not in out


def test_access_hints_are_silent_when_nothing_applies(captured: Console) -> None:
    cli_main._render_access_hints(AccessHints())

    assert captured.export_text().strip() == ""


def test_ca_hint_includes_macos_one_liner_on_darwin(
    captured: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Darwin we surface the `security add-trusted-cert` one-liner so the
    # dev doesn't have to remember the keychain incantation.
    monkeypatch.setattr(cli_main.sys, "platform", "darwin")

    cli_main._render_access_hints(AccessHints(ca_trust_hint=True))
    out = captured.export_text()

    assert "Trust the lab CA" in out
    assert "macOS one-liner" in out
    assert "security add-trusted-cert" in out


def test_ca_hint_omits_macos_one_liner_on_linux(
    captured: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On non-Darwin the `security add-trusted-cert` line is misleading (the
    # tool doesn't exist). The generic "import into your OS keychain" line
    # must still print so Linux devs aren't left without instruction.
    monkeypatch.setattr(cli_main.sys, "platform", "linux")

    cli_main._render_access_hints(AccessHints(ca_trust_hint=True))
    out = captured.export_text()

    assert "Trust the lab CA" in out
    assert "import ~/lab-ca.crt into your OS keychain" in out
    assert "macOS one-liner" not in out
    assert "security add-trusted-cert" not in out


def test_ca_hint_skipped_when_the_owning_chart_did_not_sync(captured: Console) -> None:
    cli_main._render_access_hints(AccessHints(ca_trust_hint=False, urls=("https://x/",)))

    assert "Trust the lab CA" not in captured.export_text()


# ----- down / delete --------------------------------------------------------


def test_cluster_action_reports_the_change_and_the_reaped_forward(
    captured: Console,
) -> None:
    cli_main._render_cluster_action(
        ClusterActionResult(cluster_name="chart-manager", changed=True, port_forward_pid=4242),
        verb="stopped",
        absent="not running",
    )
    out = captured.export_text()

    assert "sandbox cluster stopped: chart-manager" in out
    assert "stopped port-forward (pid 4242)" in out


def test_cluster_action_reports_the_absent_state(captured: Console) -> None:
    cli_main._render_cluster_action(
        ClusterActionResult(cluster_name="chart-manager", changed=False),
        verb="deleted",
        absent="not present",
    )
    out = captured.export_text()

    assert "sandbox cluster not present: chart-manager" in out
    assert "port-forward" not in out


# ----- markup safety and exit codes ------------------------------------------
#
# Progress messages carry raw subprocess output. Rich parses `[...]` as
# markup, and an unmatched *closing* tag raises MarkupError -- so a
# bracketed path in helm/kubectl stderr replaced the diagnostic the
# operator needed with a traceback. Opening-tag-shaped text passes through
# literally; the hazard is specifically `[/`.


@pytest.mark.parametrize(
    "message",
    [
        "cannot open [/etc/hosts]",
        "GET [/api/v1/namespaces] 503",
        'patch failed: [{"op":"remove","path":"[/spec/replicas]"}]',
        "unclosed [bold and [/nope]",
    ],
)
def test_progress_never_raises_markup_error_on_subprocess_output(
    captured: Console, message: str
) -> None:
    cli_main._print_progress(failure("failed", message))

    # Rendered literally, not swallowed and not interpreted as styling.
    assert message in captured.export_text()


def test_progress_still_styles_the_label(captured: Console) -> None:
    cli_main._print_progress(step("Applying", "grafana:minimal"))

    assert "Applying grafana:minimal" in captured.export_text()


def _result(*, failed: bool) -> LabResult:
    entry = EntryOutcome(chart="grafana", profile="minimal", namespace="obs")
    return LabResult(
        applied=[entry],
        no_change=[],
        failed=(
            [EntryFailure(chart="loki", profile="minimal", namespace="obs", error="boom")]
            if failed
            else []
        ),
        hints=AccessHints(),
    )


def test_a_converge_with_failures_exits_non_zero(captured: Console) -> None:
    """`sandbox up` rendered the failure line and then exited 0.

    LabResult.ok exists so a surface can branch on it; CI wrappers and
    `mise run lab-up` read success from a run in which charts failed.
    """
    with pytest.raises(typer.Exit) as exc:
        cli_main._exit_if_failed(_result(failed=True).ok)

    assert exc.value.exit_code == 1


def test_a_clean_converge_does_not_exit(captured: Console) -> None:
    cli_main._exit_if_failed(_result(failed=False).ok)
