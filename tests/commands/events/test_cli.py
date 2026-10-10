"""`chart-manager event emit build|promote`.

Everything here goes through the real writer seam: the whole suite clears
`EVENTS_BACKEND` (`conftest.hermetic_terminal`), and each test that cares about the
payload substitutes a recording `EventWriter` at the module seam
`commands/events/cli.py::_make_event_writer` rather than reaching into the store.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from chart_manager.commands.events import cli as events_cli
from chart_manager.plumbing.exit_codes import exit_code_for
from chart_manager.shared.events.model import BuildPhase, PromotionPhase
from chart_manager.shared.events.query import EventsDisabledError
from tests.conftest import FakeCosmosContainer, cli


class RecordingWriter:
    """An `EventWriter` stand-in that keeps the kwargs it was called with.

    A fake rather than a Mock: the assertions below are about the *values*
    the surface resolved, and a Mock would let a renamed keyword pass.
    """

    def __init__(self) -> None:
        self.build_calls: list[dict[str, Any]] = []
        self.promote_calls: list[dict[str, Any]] = []

    def build(self, **kwargs: Any) -> None:
        self.build_calls.append(kwargs)

    def promote(self, **kwargs: Any) -> None:
        self.promote_calls.append(kwargs)


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> RecordingWriter:
    """Replace the writer `_make_event_writer` builds with a recorder."""
    recorder = RecordingWriter()
    monkeypatch.setattr(events_cli, "_make_event_writer", lambda: recorder)
    return recorder


# --------------------------------------------------------------------------
# the positional, which is the point of the command
# --------------------------------------------------------------------------


def test_build_splits_the_positional_into_chart_and_version(
    writer: RecordingWriter,
) -> None:
    result = cli("event", "emit", "build", "grafana@1.2.3", "--phase", "published")

    assert result.exit_code == 0
    assert writer.build_calls == [
        {
            "chart_name": "grafana",
            "chart_version": "1.2.3",
            "phase": BuildPhase.PUBLISHED,
            "build_correlation_id": None,
            "pr_url": None,
            "git_sha": None,
            "timestamp": None,
        }
    ]


def test_promote_splits_the_positional_and_keeps_the_environment(
    writer: RecordingWriter,
) -> None:
    result = cli(
        "event", "emit", "promote", "grafana@1.2.3",
        "--env", "staging", "--phase", "reached_prod",
    )

    assert result.exit_code == 0
    call = writer.promote_calls[0]
    assert call["chart_name"] == "grafana"
    assert call["chart_version"] == "1.2.3"
    assert call["environment"] == "staging"
    assert call["phase"] is PromotionPhase.REACHED_PROD


def test_the_optional_fields_still_reach_the_writer(writer: RecordingWriter) -> None:
    """The positional replaced two flags; it must not have eaten the rest."""
    cli(
        "event", "emit", "build", "grafana@1.2.3",
        "--phase", "merged",
        "--build-correlation-id", "org/charts#7",
        "--pr-url", "https://example.invalid/pr/7",
        "--git-sha", "deadbeef",
        "--at", "2026-07-30T12:00:00Z",
    )

    assert writer.build_calls[0] == {
        "chart_name": "grafana",
        "chart_version": "1.2.3",
        "phase": BuildPhase.MERGED,
        "build_correlation_id": "org/charts#7",
        "pr_url": "https://example.invalid/pr/7",
        "git_sha": "deadbeef",
        "timestamp": datetime(2026, 7, 30, 12, 0, tzinfo=UTC),
    }


def test_a_non_utc_at_offset_is_normalized_to_utc(writer: RecordingWriter) -> None:
    """Stored timestamps are isoformat strings; only UTC stamps compare
    chronologically against the UTC stamps every live emitter writes."""
    cli(
        "event", "emit", "build", "grafana@1.2.3",
        "--phase", "merged", "--at", "2026-07-30T14:00:00+02:00",
    )

    assert writer.build_calls[0]["timestamp"] == datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "argv",
    [
        ("grafana", "--phase", "published"),
        ("@1.2.3", "--phase", "published"),
        ("a@b@c", "--phase", "published"),
        ("grafana@", "--phase", "published"),
        ("--phase", "published"),
        # A backfill from a laptop in another timezone must not shift history.
        ("grafana@1.2.3", "--phase", "merged", "--at", "2026-07-30T12:00:00"),
    ],
    ids=[
        "no-version", "no-chart", "two-separators", "empty-version", "missing-ref", "naive-at",
    ],
)
def test_an_invalid_emit_is_a_usage_error(
    writer: RecordingWriter, argv: tuple[str, ...]
) -> None:
    """A bad ref or a timestamp with no timezone exits 2 and writes nothing,
    so no record is stored under a guessed value.
    """
    result = cli("event", "emit", "build", *argv)

    assert result.exit_code == 2
    assert writer.build_calls == []


# --------------------------------------------------------------------------
# failure policy and streams, unchanged by the restructuring
# --------------------------------------------------------------------------


def test_a_failed_emit_is_non_fatal_and_confirms_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telemetry must not break the run that produced it."""

    class Exploding:
        def build(self, **kwargs: Any) -> None:
            raise RuntimeError("backend down")

    monkeypatch.setattr(events_cli, "_make_event_writer", Exploding)

    result = cli("event", "emit", "build", "grafana@1.2.3", "--phase", "published")

    assert result.exit_code == 0
    assert "emitted" not in result.stderr


def test_strict_turns_a_failed_emit_into_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Exploding:
        def build(self, **kwargs: Any) -> None:
            raise RuntimeError("backend down")

    monkeypatch.setattr(events_cli, "_make_event_writer", Exploding)

    result = cli(
        "event", "emit", "build", "grafana@1.2.3", "--phase", "published", "--strict-events"
    )

    assert result.exit_code != 0


def test_the_confirmation_is_narration_not_data(writer: RecordingWriter) -> None:
    """`event emit` has no `--output` projection, so stdout stays empty."""
    result = cli("event", "emit", "build", "grafana@1.2.3", "--phase", "published")

    assert result.stdout == ""
    assert "emitted build:published for grafana@1.2.3" in result.stderr


def test_the_confirmation_names_the_ref_in_its_wire_form(
    writer: RecordingWriter,
) -> None:
    """The summary quotes `chart@version` -- the correlation id, not two words."""
    result = cli(
        "event", "emit", "promote", "grafana@1.2.3",
        "--env", "dev", "--phase", "promoted",
    )

    assert "emitted promote:promoted for grafana@1.2.3 -> dev" in result.stderr


# --------------------------------------------------------------------------
# the command tree
# --------------------------------------------------------------------------


def test_the_group_is_singular_and_nests_emit() -> None:
    """`event`, matching `chart` and `promote`."""
    result = cli("event", "--help")

    assert result.exit_code == 0
    assert "emit" in result.stdout


def test_the_pre_emit_spelling_is_gone() -> None:
    """`events build` was an alias of `event emit build`. Aliases are deleted.

    Absent, not hidden and not an empty group: a `events` that still parsed
    would keep CI green on a spelling the docs no longer mention, which is
    exactly the silent-drift the alias removal was for. Asserted by running
    the CLI rather than by inspecting the command tree, because what matters
    is what a caller's argv does.
    """
    result = cli("events", "build", "grafana@1.2.3")

    assert result.exit_code != 0
    assert "No such command" in result.output


# --------------------------------------------------------------------------
# `event emit --dry-run`
# --------------------------------------------------------------------------


def test_dry_run_prints_the_composed_document_and_confirms_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point: see the exact stored document with no backend configured.

    Backend resolution is rigged to fail, so the test proves --dry-run never
    reaches it; EVENTS_BACKEND is unset, which is the shipped default.
    """
    monkeypatch.setattr(
        "chart_manager.cli._container.get_event_store",
        lambda settings: pytest.fail("--dry-run resolved an event store"),
    )

    result = cli(
        "event", "emit", "build", "grafana@1.2.3",
        "--phase", "published", "--git-sha", "deadbeef",
        "--at", "2026-07-30T14:00:00+02:00",
        "--dry-run", "-o", "json",
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["correlation_id"] == "grafana@1.2.3"
    assert document["chart_name"] == "grafana"
    assert document["build_phase"] == "published"
    assert document["git_sha"] == "deadbeef"
    # The --at offset is already normalized in what would be written.
    assert document["timestamp"] == "2026-07-30T12:00:00+00:00"
    assert "emitted" not in result.stderr


def test_dry_run_promote_carries_the_environment_and_phase() -> None:
    result = cli(
        "event", "emit", "promote", "grafana@1.2.3",
        "--env", "staging", "--phase", "promoted", "--dry-run", "-o", "json",
    )

    document = json.loads(result.stdout)
    assert document["environment"] == "staging"
    assert document["promotion_phase"] == "promoted"
    assert document["build_phase"] is None


def test_dry_run_has_a_table_projection_for_a_human() -> None:
    result = cli(
        "event", "emit", "build", "grafana@1.2.3",
        "--phase", "published", "--dry-run", "-o", "table",
    )

    assert result.exit_code == 0
    assert "correlation_id" in result.stdout
    assert "grafana@1.2.3" in result.stdout


def test_output_without_dry_run_is_a_usage_error(writer: RecordingWriter) -> None:
    """A real emit has no projection; accepting -o and ignoring it would lie."""
    result = cli(
        "event", "emit", "build", "grafana@1.2.3", "--phase", "published", "-o", "json"
    )

    assert result.exit_code == 2
    assert writer.build_calls == []


# --------------------------------------------------------------------------
# the read side: `event list`
# --------------------------------------------------------------------------


_EVENT_DOC: dict[str, Any] = {
    "chart_name": "grafana",
    "chart_version": "1.2.3",
    "correlation_id": "grafana@1.2.3",
    "build_phase": None,
    "promotion_phase": "promoted",
    "environment": "staging",
    "source": "chart-manager",
    "timestamp": "2026-08-01T12:00:00+00:00",
}


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch):
    """Replace the read-side seam with a recorder over scripted documents."""

    class RecordingReader:
        def __init__(self) -> None:
            self.queries: list[Any] = []
            self.events: list[dict[str, Any]] = [dict(_EVENT_DOC)]

        def __call__(self, request: Any) -> list[dict[str, Any]]:
            self.queries.append(request)
            return list(self.events)

    recorder = RecordingReader()
    monkeypatch.setattr(events_cli, "_query_events", recorder)
    return recorder


def test_list_with_no_selector_asks_for_recent_activity_across_all_charts(
    reader,
) -> None:
    result = cli("event", "list")

    assert result.exit_code == 0
    (query,) = reader.queries
    assert (query.chart_name, query.correlation_id) == (None, None)


def test_list_with_a_bare_chart_selects_that_charts_history(reader) -> None:
    cli("event", "list", "grafana")

    (query,) = reader.queries
    assert (query.chart_name, query.correlation_id) == ("grafana", None)


def test_list_with_a_versioned_selector_selects_one_release_timeline(reader) -> None:
    cli("event", "list", "grafana@1.2.3")

    (query,) = reader.queries
    assert (query.chart_name, query.correlation_id) == ("grafana", "grafana@1.2.3")


def test_list_passes_the_limit_through_and_defaults_it(reader) -> None:
    from chart_manager.shared.events.query import DEFAULT_LIMIT

    cli("event", "list")
    cli("event", "list", "-n", "5")

    assert [query.limit for query in reader.queries] == [DEFAULT_LIMIT, 5]


def test_a_malformed_selector_is_a_usage_error(reader) -> None:
    result = cli("event", "list", "a@b@c")

    assert result.exit_code == 2
    assert reader.queries == []


def test_the_table_is_the_human_recent_activity_view(reader) -> None:
    result = cli("event", "list", "-o", "table")

    for column in ("Chart", "Version", "Phase", "Env", "PR", "Source", "Timestamp", "Age"):
        assert column in result.stdout
    assert "grafana" in result.stdout
    assert "promoted" in result.stdout
    assert "staging" in result.stdout
    assert "2026-08-01 12:00:00Z" in result.stdout


def test_the_table_shows_the_pr_url(reader) -> None:
    reader.events = [
        dict(_EVENT_DOC, pr_url="https://github.com/garyellis/lab-charts/pull/38"),
        dict(_EVENT_DOC),
    ]

    result = cli("event", "list", "-o", "table")

    assert "pull/38" in result.stdout.replace("\n", "")


def test_list_against_a_disabled_backend_says_how_to_enable_events() -> None:
    """conftest clears EVENTS_BACKEND; `none` is the shipped default."""
    result = cli("event", "list")

    assert isinstance(result.exception, EventsDisabledError)
    assert exit_code_for(result.exception.outcome) == 5
    assert "EVENTS_BACKEND" in str(result.exception)
    assert result.stdout == ""


def test_list_json_is_the_events_newest_first_across_mixed_timezone_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A +02:00 stamp that is older in real time must not lead the listing
    just because it string-sorts newer. The fake container returns the
    backend's string order."""
    offset = dict(_EVENT_DOC, chart_name="older", timestamp="2026-08-01T14:30:00+02:00")
    utc = dict(_EVENT_DOC, chart_name="newer", timestamp="2026-08-01T13:00:00+00:00")
    container = FakeCosmosContainer(documents=[offset, utc])  # string order: +02:00 first

    monkeypatch.setenv("EVENTS_BACKEND", "cosmos")
    monkeypatch.setattr("chart_manager.integrations.cosmos.get_container", lambda *_: container)

    result = cli("event", "list", "-o", "json")

    assert (result.exit_code, json.loads(result.stdout)) == (0, {"events": [utc, offset]})
