"""`chart-manager event emit build|promote`.

The emit path had no CLI coverage before P1.5, which is why the restructuring
lands with it. Everything here goes through the real writer seam: the whole
suite pins `EVENTS_BACKEND=none` (`conftest.hermetic_terminal`), and each test
that cares about the payload substitutes a recording `EventWriter` at the
module seam `cli/events.py::_make_event_writer` rather than reaching into the
store.

Alias equivalence for the old `events build|promote` spelling is not asserted
here -- `tests/test_cli_aliases.py` owns that property for every alias at
once. What *is* asserted here is the part the alias gate cannot see: that the
deprecated `--chart` / `--version` flag pair still resolves to the same ref as
the positional.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from chart_manager.cli import events as events_cli
from chart_manager.plumbing.exit_codes import EXIT_ENVIRONMENT
from chart_manager.services.events.lifecycle import BuildPhase, PromotionPhase
from chart_manager.services.events.ref import SEPARATOR

from .conftest import cli


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
    """Replace the composition-root writer with a recorder."""
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


def test_a_naive_at_timestamp_is_a_usage_error(writer: RecordingWriter) -> None:
    """A backfill from a laptop in another timezone must not shift history."""
    result = cli(
        "event", "emit", "build", "grafana@1.2.3",
        "--phase", "merged", "--at", "2026-07-30T12:00:00",
    )

    assert result.exit_code == 2
    assert writer.build_calls == []


@pytest.mark.parametrize(
    "token",
    ["grafana", "@1.2.3", "a@b@c", "grafana@"],
    ids=["no-version", "no-chart", "two-separators", "empty-version"],
)
def test_a_malformed_ref_is_a_usage_error(writer: RecordingWriter, token: str) -> None:
    """Exit 2 with usage, matching how `--at` already reports a bad value.

    Crucially, nothing is written: a ref the system cannot address must not
    produce a ledger record under a guessed correlation id.
    """
    result = cli("event", "emit", "build", token, "--phase", "published")

    assert result.exit_code == 2
    assert writer.build_calls == []


def _cli_events_ast() -> ast.Module:
    """`cli/events.py` parsed, for the two structural assertions below."""
    assert events_cli.__file__ is not None
    return ast.parse(Path(events_cli.__file__).read_text(encoding="utf-8"))


def test_the_surface_delegates_the_grammar_to_the_service() -> None:
    """Design commitment 6, half one: the resolver is imported, not inlined."""
    imported = {
        alias.name
        for node in ast.walk(_cli_events_ast())
        if isinstance(node, ast.ImportFrom)
        and node.module == "chart_manager.services.events.ref"
        for alias in node.names
    }

    assert {"parse_ref", "ref_from_parts"} <= imported


def test_the_surface_never_names_the_separator() -> None:
    """Design commitment 6, half two, and the part a reviewer would miss.

    A bare `"@"` constant in `cli/` means the surface is composing or
    splitting the ref itself -- and an f-string like `f"{chart}@{version}"`
    lowers to exactly that constant in the AST, so this catches the tempting
    shortcut as well as an explicit `.split("@")`. A second surface (REST,
    Slack) must not be able to disagree with this one about what `a@b@c`
    means. Whole tokens such as `"CHART@VERSION"` are unaffected.
    """
    offenders = [
        node.lineno
        for node in ast.walk(_cli_events_ast())
        if isinstance(node, ast.Constant) and node.value == SEPARATOR
    ]

    assert not offenders, (
        f"cli/events.py handles the ref separator itself at line(s) {offenders}; "
        "the grammar belongs to services/events/ref.py"
    )


# --------------------------------------------------------------------------
# the deprecated flag form (design doc 5: "flags accepted as alias")
# --------------------------------------------------------------------------


@pytest.mark.parametrize("version_flag", ["--version", "--chart-version"])
def test_the_flag_pair_resolves_to_the_same_ref_as_the_positional(
    writer: RecordingWriter, version_flag: str
) -> None:
    """`--version` is the flag actually being aliased; `--chart-version` is
    its replacement name, matching the schema field (design doc 7.5)."""
    cli(
        "event", "emit", "build",
        "--chart", "grafana", version_flag, "1.2.3",
        "--phase", "published",
    )
    cli("event", "emit", "build", "grafana@1.2.3", "--phase", "published")

    assert writer.build_calls[0] == writer.build_calls[1]


def test_the_flag_pair_works_on_promote_too(writer: RecordingWriter) -> None:
    cli(
        "event", "emit", "promote",
        "--chart", "grafana", "--version", "1.2.3",
        "--env", "dev", "--phase", "promoted",
    )

    assert writer.promote_calls[0]["chart_name"] == "grafana"
    assert writer.promote_calls[0]["chart_version"] == "1.2.3"


def test_the_deprecated_flags_are_hidden_from_help() -> None:
    """A deprecated spelling that advertises itself recruits new callers."""
    result = cli("event", "emit", "build", "--help")

    assert "CHART@VERSION" in result.stdout
    assert "--chart " not in result.stdout
    assert "--chart-version" not in result.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ("--chart", "grafana", "--phase", "published"),
        ("--chart-version", "1.2.3", "--phase", "published"),
        ("--phase", "published"),
    ],
    ids=["chart-without-version", "version-without-chart", "neither"],
)
def test_an_incomplete_selection_is_a_usage_error(
    writer: RecordingWriter, argv: tuple[str, ...]
) -> None:
    result = cli("event", "emit", "build", *argv)

    assert result.exit_code == 2
    assert writer.build_calls == []


def test_the_positional_and_the_flags_may_not_be_combined(
    writer: RecordingWriter,
) -> None:
    """Silently preferring one would make the ignored one a lie."""
    result = cli(
        "event", "emit", "build", "grafana@1.2.3",
        "--chart", "loki", "--phase", "published",
    )

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

    # `--strict`, not `--strict-events`: P1.3 owns that rename.
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
    """`event`, matching `chart` and `helmrelease` (design commitment 1)."""
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
    from chart_manager.services.events.query import DEFAULT_LIMIT

    cli("event", "list")
    cli("event", "list", "-n", "5")

    assert [query.limit for query in reader.queries] == [DEFAULT_LIMIT, 5]


def test_a_malformed_selector_is_a_usage_error(reader) -> None:
    result = cli("event", "list", "a@b@c")

    assert result.exit_code == 2
    assert reader.queries == []


def test_the_table_is_the_human_recent_activity_view(reader) -> None:
    result = cli("event", "list", "-o", "table")

    for column in ("Chart", "Version", "Phase", "Env", "Source", "Age"):
        assert column in result.stdout
    assert "grafana" in result.stdout
    assert "promoted" in result.stdout
    assert "staging" in result.stdout


def test_the_json_projection_is_the_versioned_wire_document(reader) -> None:
    from chart_manager.services.events.wire import SCHEMA_VERSION

    result = cli("event", "list", "grafana@1.2.3", "-o", "json")

    payload = json.loads(result.stdout)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["chart"] == "grafana"
    assert payload["correlation_id"] == "grafana@1.2.3"
    assert payload["count"] == 1
    assert payload["events"] == [_EVENT_DOC]


def test_list_against_a_disabled_backend_says_how_to_enable_events() -> None:
    """conftest pins EVENTS_BACKEND=none, which is also the shipped default."""
    result = cli("event", "list")

    assert result.exit_code == EXIT_ENVIRONMENT
    assert "EVENTS_BACKEND" in result.stderr
    assert result.stdout == ""


def test_list_against_dynamodb_says_the_read_side_is_cosmos_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENTS_BACKEND", "dynamodb")

    result = cli("event", "list")

    assert result.exit_code == EXIT_ENVIRONMENT
    assert "Cosmos-only" in result.stderr
    assert "query-events-dynamodb" in result.stderr


def test_list_renders_newest_first_across_mixed_timezone_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the real dispatch: a +02:00 stamp that is *older*
    in real time must not lead the listing just because it string-sorts
    newer. The fake container returns the backend's string order."""

    class FakeContainer:
        def query_items(self, **kwargs: Any) -> list[dict[str, Any]]:
            offset = dict(_EVENT_DOC, chart_name="older", timestamp="2026-08-01T14:30:00+02:00")
            utc = dict(_EVENT_DOC, chart_name="newer", timestamp="2026-08-01T13:00:00+00:00")
            return [offset, utc]  # string order: +02:00 first

    from chart_manager.services.events import store as store_module

    monkeypatch.setenv("EVENTS_BACKEND", "cosmos")
    monkeypatch.setattr(store_module, "get_container", lambda **kwargs: FakeContainer())

    result = cli("event", "list", "-o", "json")

    charts = [event["chart_name"] for event in json.loads(result.stdout)["events"]]
    assert charts == ["newer", "older"]
