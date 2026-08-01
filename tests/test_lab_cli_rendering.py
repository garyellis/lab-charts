"""CLI rendering of the results the lab/sandbox services now return.

`cli/local.py` is where the `local` group's Rich rendering lives -- the
service layer knows nothing about a terminal. These tests pin
the output shape so the services-return-results refactor stayed a refactor:
the summary table, the access-hint blocks, the lifecycle lines and the
progress narration must carry the same information they did when the
services printed them themselves.

`cli/streams.py` owns two consoles: `console` for the selected output
projection and `narration` for everything else. These tests record them
separately, so which fixture a test asks for *is* the assertion about which
stream the text lands on. `tests/test_output_streams.py` owns the general
rule; these pin the per-block content.
"""

from __future__ import annotations

import pytest
import typer
from rich.console import Console

from chart_manager.cli import local as cli_local
from chart_manager.cli import streams
from chart_manager.services.clusters.development import (
    DevelopmentClusterAccessHints,
    DevelopmentClusterActionResult,
    DevelopmentClusterEntryFailure,
    DevelopmentClusterEntryOutcome,
    DevelopmentClusterResult,
)
from chart_manager.services.progress import detail, failure, info, step, warn


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Console:
    """Swap the module's *data* console (stdout) for a recording one."""
    console = Console(record=True, width=200)
    monkeypatch.setattr(cli_local, "console", console)
    return console


@pytest.fixture
def narrated(monkeypatch: pytest.MonkeyPatch) -> Console:
    """Swap the *narration* console (stderr) for a recording one.

    Patched in two places because the consoles now live in `cli/streams.py`
    and each consumer binds its own module-level name to the same object:
    `cli_local.narration` is what the renderers below reach, and
    `streams.narration` is what `streams.print_progress` reaches. Patching
    one and not the other would silently record half the output.
    """
    console = Console(record=True, width=200)
    monkeypatch.setattr(cli_local, "narration", console)
    monkeypatch.setattr(streams, "narration", console)
    return console


# ----- progress event rendering ---------------------------------------------


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            step("Applying", "grafana:minimal -> observability"),
            "Applying grafana:minimal -> observability",
        ),
        (step("Waiting for kube-apiserver"), "Waiting for kube-apiserver"),
        (
            detail("skip", "grafana:minimal (already installed in observability)"),
            "skip grafana:minimal (already installed in observability)",
        ),
        (warn("could not list helm releases"), "warn: could not list helm releases"),
        (warn("cilium chart not found", label=None), "cilium chart not found"),
        (
            failure("apply failed:", "grafana:minimal -> boom"),
            "apply failed: grafana:minimal -> boom",
        ),
        (info("pod/foo   0/1   CrashLoopBackOff"), "pod/foo   0/1   CrashLoopBackOff"),
    ],
)
def test_progress_events_render_label_then_message(
    narrated: Console, event: object, expected: str
) -> None:
    streams.print_progress(event)  # type: ignore[arg-type]

    assert narrated.export_text().strip() == expected


# ----- summary table --------------------------------------------------------


def test_lab_result_renders_every_bucket(captured: Console, narrated: Console) -> None:
    """The table is the projection; the failure tally narrates alongside it."""
    result = DevelopmentClusterResult(
        applied=(DevelopmentClusterEntryOutcome("grafana", "minimal", "observability"),),
        no_change=(DevelopmentClusterEntryOutcome("loki", "minimal", "observability"),),
        failed=(DevelopmentClusterEntryFailure("mimir", "minimal", "observability", "boom"),),
    )

    cli_local._render_development_cluster_result(result, "table", command="up")
    out = captured.export_text()

    assert "Lab install summary" in out
    for token in ("applied", "grafana", "no-change", "loki", "failed", "mimir"):
        assert token in out
    # Not in `out`: a tally is not part of the table a caller pipes.
    assert "1 chart(s) failed" in narrated.export_text()
    assert "chart(s) failed" not in out


def test_lab_result_omits_the_failure_line_when_ok(
    captured: Console, narrated: Console
) -> None:
    cli_local._render_development_cluster_result(
        DevelopmentClusterResult(
            applied=(DevelopmentClusterEntryOutcome("grafana", "minimal", "observability"),)
        ),
        "table",
        command="up",
    )

    assert "chart(s) failed" not in narrated.export_text()
    assert "chart(s) failed" not in captured.export_text()


# ----- access hints ---------------------------------------------------------


def test_access_hints_render_urls_and_grafana_credentials(narrated: Console) -> None:
    cli_local._render_access_hints(
        DevelopmentClusterAccessHints(
            urls=("https://grafana.localhost/", "https://loki.localhost/"),
            grafana_url="https://grafana.localhost/",
            grafana_credentials=("admin", "s3cret"),
        )
    )
    out = narrated.export_text()

    assert "URLs:" in out
    # Sort order is the service's; the renderer must not reshuffle it.
    assert out.index("grafana.localhost") < out.index("loki.localhost")
    assert "user: admin" in out
    assert "s3cret" in out


def test_access_hints_render_the_secret_read_failure_in_place(narrated: Console) -> None:
    cli_local._render_access_hints(
        DevelopmentClusterAccessHints(
            urls=("https://grafana.localhost/",),
            grafana_url="https://grafana.localhost/",
            grafana_error="secret not found",
        )
    )
    out = narrated.export_text()

    assert "could not read admin password" in out
    assert "secret not found" in out
    assert "user: admin" not in out


def test_access_hints_render_the_virtualservice_listing_failure(narrated: Console) -> None:
    cli_local._render_access_hints(
        DevelopmentClusterAccessHints(
            urls_error="could not list VirtualServices (boom); skipping URL hints"
        )
    )
    out = narrated.export_text()

    assert "warn:" in out
    assert "could not list VirtualServices" in out
    assert "URLs:" not in out


def test_access_hints_are_silent_when_nothing_applies(narrated: Console) -> None:
    cli_local._render_access_hints(DevelopmentClusterAccessHints())

    assert narrated.export_text().strip() == ""


def test_ca_hint_includes_macos_one_liner_on_darwin(
    narrated: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Darwin we surface the `security add-trusted-cert` one-liner so the
    # dev doesn't have to remember the keychain incantation.
    monkeypatch.setattr(cli_local.sys, "platform", "darwin")

    cli_local._render_access_hints(DevelopmentClusterAccessHints(ca_trust_hint=True))
    out = narrated.export_text()

    assert "Trust the lab CA" in out
    assert "macOS one-liner" in out
    assert "security add-trusted-cert" in out


def test_ca_hint_omits_macos_one_liner_on_linux(
    narrated: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On non-Darwin the `security add-trusted-cert` line is misleading (the
    # tool doesn't exist). The generic "import into your OS keychain" line
    # must still print so Linux devs aren't left without instruction.
    monkeypatch.setattr(cli_local.sys, "platform", "linux")

    cli_local._render_access_hints(DevelopmentClusterAccessHints(ca_trust_hint=True))
    out = narrated.export_text()

    assert "Trust the lab CA" in out
    assert "import ~/lab-ca.crt into your OS keychain" in out
    assert "macOS one-liner" not in out
    assert "security add-trusted-cert" not in out


def test_ca_hint_skipped_when_the_owning_chart_did_not_sync(narrated: Console) -> None:
    cli_local._render_access_hints(
        DevelopmentClusterAccessHints(ca_trust_hint=False, urls=("https://x/",))
    )

    assert "Trust the lab CA" not in narrated.export_text()


# ----- down / delete --------------------------------------------------------


def test_cluster_action_reports_the_change_and_the_reaped_forward(
    narrated: Console,
) -> None:
    cli_local._render_cluster_action(
        DevelopmentClusterActionResult(
            cluster_name="chart-manager", changed=True, port_forward_pid=4242
        ),
        "table",
        command="down",
        verb="stopped",
        absent="not running",
    )
    out = narrated.export_text()

    assert "local cluster stopped: chart-manager" in out
    assert "stopped port-forward (pid 4242)" in out


def test_cluster_action_reports_the_absent_state(narrated: Console) -> None:
    cli_local._render_cluster_action(
        DevelopmentClusterActionResult(cluster_name="chart-manager", changed=False),
        "table",
        command="down",
        verb="deleted",
        absent="not present",
    )
    out = narrated.export_text()

    assert "local cluster not present: chart-manager" in out
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
    narrated: Console, message: str
) -> None:
    streams.print_progress(failure("failed", message))

    # Rendered literally, not swallowed and not interpreted as styling.
    assert message in narrated.export_text()


def test_progress_still_styles_the_label(narrated: Console) -> None:
    streams.print_progress(step("Applying", "grafana:minimal"))

    assert "Applying grafana:minimal" in narrated.export_text()


def _result(*, failed: bool) -> DevelopmentClusterResult:
    entry = DevelopmentClusterEntryOutcome(chart="grafana", profile="minimal", namespace="obs")
    return DevelopmentClusterResult(
        applied=[entry],
        no_change=[],
        failed=(
            [
                DevelopmentClusterEntryFailure(
                    chart="loki", profile="minimal", namespace="obs", error="boom"
                )
            ]
            if failed
            else []
        ),
        hints=DevelopmentClusterAccessHints(),
    )


def test_a_converge_with_failures_exits_non_zero(captured: Console) -> None:
    """`local up` rendered the failure line and then exited 0.

    DevelopmentClusterResult.ok exists so a surface can branch on it; CI wrappers and
    `mise run lab-up` read success from a run in which charts failed.
    """
    with pytest.raises(typer.Exit) as exc:
        cli_local._exit_if_failed(_result(failed=True).ok)

    assert exc.value.exit_code == 1


def test_a_clean_converge_does_not_exit(captured: Console) -> None:
    cli_local._exit_if_failed(_result(failed=False).ok)
