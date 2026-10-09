"""CLI rendering of the results `commands/local/run.py` returns.

`commands/local/cli.py` is where the `local` group's Rich rendering lives --
`run` knows nothing about a terminal. These tests pin the output shape: the
summary table, the access-hint blocks and the lifecycle lines.

`cli/streams.py` owns two consoles: `console` for the selected output
projection and `narration` for everything else. These tests record them
separately, so which fixture a test asks for *is* the assertion about which
stream the text lands on. `tests/test_output_streams.py` owns the general
rule; these pin the per-block content.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from chart_manager.commands.local import cli as cli_local
from chart_manager.commands.local.models import (
    DevClusterAccessHints,
    DevClusterActionResult,
    DevClusterCredentials,
    DevClusterEntryFailure,
    DevClusterEntryOutcome,
    DevClusterResult,
)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Console:
    """Swap the module's *data* console (stdout) for a recording one."""
    console = Console(record=True, width=200)
    monkeypatch.setattr(cli_local, "console", console)
    return console


@pytest.fixture
def narrated(monkeypatch: pytest.MonkeyPatch) -> Console:
    """Swap the *narration* console (stderr) for a recording one."""
    console = Console(record=True, width=200)
    monkeypatch.setattr(cli_local, "narration", console)
    return console


# ----- summary table --------------------------------------------------------


def test_dev_cluster_result_renders_every_bucket(captured: Console) -> None:
    result = DevClusterResult(
        applied=(DevClusterEntryOutcome("grafana", "minimal", "observability"),),
        no_change=(DevClusterEntryOutcome("loki", "minimal", "observability"),),
        failed=(DevClusterEntryFailure("mimir", "minimal", "observability", "boom"),),
    )

    cli_local._print_converge(result)
    out = captured.export_text()

    assert "Dev cluster install summary" in out
    for token in ("applied", "grafana", "no-change", "loki", "failed", "mimir"):
        assert token in out


# ----- access hints ---------------------------------------------------------


def test_access_hints_render_credentials_under_their_url(narrated: Console) -> None:
    cli_local._render_access_hints(
        DevClusterAccessHints(
            urls=("https://app.localhost/", "https://loki.localhost/"),
            credentials=(
                DevClusterCredentials(
                    url="https://app.localhost/", username="admin", password="s3cret"
                ),
            ),
        )
    )
    out = narrated.export_text()

    assert "URLs:" in out
    # Sort order is `run`'s; the renderer must not reshuffle it.
    assert out.index("app.localhost") < out.index("user: admin") < out.index("loki.localhost")
    assert "pass: s3cret" in out


def test_access_hints_render_the_credential_failure_in_place(narrated: Console) -> None:
    cli_local._render_access_hints(
        DevClusterAccessHints(
            urls=("https://app.localhost/", "https://loki.localhost/"),
            credentials=(
                DevClusterCredentials(
                    url="https://app.localhost/", error="secret not found"
                ),
            ),
        )
    )
    out = narrated.export_text()

    assert "could not read credentials: secret not found" in out
    assert out.index("app.localhost") < out.index("secret not found") < out.index("loki")
    assert "user:" not in out


def test_access_hints_render_the_virtualservice_listing_failure(narrated: Console) -> None:
    cli_local._render_access_hints(
        DevClusterAccessHints(
            urls_error="could not list VirtualServices (boom); skipping URL hints"
        )
    )
    out = narrated.export_text()

    assert "warn:" in out
    assert "could not list VirtualServices" in out
    assert "URLs:" not in out


def test_access_hints_are_silent_when_nothing_applies(narrated: Console) -> None:
    cli_local._render_access_hints(DevClusterAccessHints())

    assert narrated.export_text().strip() == ""


def test_ca_hint_includes_macos_one_liner_on_darwin(
    narrated: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Darwin we surface the `security add-trusted-cert` one-liner so the
    # dev doesn't have to remember the keychain incantation.
    monkeypatch.setattr(cli_local.sys, "platform", "darwin")

    cli_local._render_access_hints(DevClusterAccessHints(ca_trust_hint=True))
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

    cli_local._render_access_hints(DevClusterAccessHints(ca_trust_hint=True))
    out = narrated.export_text()

    assert "Trust the lab CA" in out
    assert "import ~/lab-ca.crt into your OS keychain" in out
    assert "macOS one-liner" not in out
    assert "security add-trusted-cert" not in out


def test_ca_hint_skipped_when_the_owning_chart_did_not_sync(narrated: Console) -> None:
    cli_local._render_access_hints(
        DevClusterAccessHints(ca_trust_hint=False, urls=("https://x/",))
    )

    assert "Trust the lab CA" not in narrated.export_text()


# ----- down / delete --------------------------------------------------------


def test_cluster_action_reports_the_change(narrated: Console) -> None:
    cli_local._print_cluster_action(DevClusterActionResult(changed=True))
    out = narrated.export_text()

    assert "dev cluster stopped: chart-manager" in out


def test_cluster_action_reports_the_absent_state(narrated: Console) -> None:
    cli_local._print_cluster_action(DevClusterActionResult(changed=False))
    out = narrated.export_text()

    assert "dev cluster not running: chart-manager" in out

