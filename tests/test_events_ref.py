"""The `CHART@VERSION` grammar, which the surface is not allowed to own.

`services/events/ref.py` exists so that parsing the token is a domain rule
(design commitment 6). These tests pin the rules the module docstring states,
including the ones that are deliberate *rejections* -- a grammar that silently
accepts `a@b@c` by guessing a split would write a wrong partition key into a
ledger nobody re-reads until an incident.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.events.lifecycle import PlatformLifecycleEvent
from chart_manager.services.events.ref import (
    ChartRef,
    ChartRefError,
    ChartSelector,
    parse_ref,
    parse_selector,
    ref_from_parts,
)


def test_a_well_formed_ref_splits_into_its_two_halves() -> None:
    ref = parse_ref("grafana@1.2.3")

    assert (ref.name, ref.version) == ("grafana", "1.2.3")


@pytest.mark.parametrize(
    "token",
    [
        "grafana@1.2.3-rc.1",
        "grafana@1.2.3+build.5",
        "kube-prometheus-stack@0.1.0",
    ],
    ids=["prerelease", "build-metadata", "hyphenated-name"],
)
def test_semver_and_rfc1123_shapes_round_trip(token: str) -> None:
    """The characters a real chart name and a real version actually use."""
    assert str(parse_ref(token)) == token


def test_the_rendered_ref_is_exactly_the_event_correlation_id() -> None:
    """The whole reason the token is not two flags.

    `EventWriter` composes `correlation_id` as `f"{chart_name}@{chart_version}"`.
    If `ChartRef.__str__` ever diverged from that, the surface would be
    accepting a token that does not name the record it creates.
    """
    ref = parse_ref("grafana@1.2.3")

    event = PlatformLifecycleEvent(
        correlation_id=f"{ref.name}@{ref.version}",
        build_correlation_id=None,
        promotion_correlation_id=None,
        chart_name=ref.name,
        chart_version=ref.version,
        images=(),
        environment=None,
        build_phase=None,
        promotion_phase=None,
        timestamp=datetime.now(UTC),
        source="test",
        pr_url=None,
        git_sha=None,
        detail=None,
    )

    assert str(ref) == event.correlation_id


def test_surrounding_whitespace_is_stripped() -> None:
    """CI passes these through shell variables; a stray space is transport."""
    assert parse_ref("  grafana @ 1.2.3  ") == ChartRef(name="grafana", version="1.2.3")


# --------------------------------------------------------------------------
# the rejections, one test per documented rule
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "because"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("grafana", "no version"),
        ("grafana@", "empty version"),
        ("grafana@   ", "empty version"),
        ("@1.2.3", "no chart"),
        ("   @1.2.3", "no chart"),
        ("grafana@1.2.3@4", "two separators"),
        ("a@b@c", "two separators"),
        ("graf ana@1.2.3", "whitespace inside the name"),
        ("grafana@1.2 .3", "whitespace inside the version"),
    ],
)
def test_malformed_refs_are_rejected(token: str, because: str) -> None:
    with pytest.raises(ChartRefError):
        parse_ref(token)


def test_a_bare_chart_name_is_rejected_rather_than_versionless() -> None:
    """Rule 2: `chart_version` is nullable in the schema, but not from here.

    A versionless ref would compose `correlation_id = "grafana@None"`, a join
    key that joins nothing. The flag this replaced (`--version`) was required,
    so this is not a new restriction -- and the error must say what to type.
    """
    with pytest.raises(ChartRefError, match="has no version"):
        parse_ref("grafana")


def test_a_version_without_a_chart_says_why() -> None:
    """Rule 3 / design doc 7.5. `store.py` partitions on `chart_name`, so a
    bare version is a cross-partition scan -- the caller needs to know that,
    not just that the string was rejected."""
    with pytest.raises(ChartRefError, match="version with no chart"):
        parse_ref("@1.2.3")


def test_multiple_separators_are_an_error_not_an_ambiguous_split() -> None:
    """Rule 1: neither half may contain `@`, so there is nothing to guess."""
    with pytest.raises(ChartRefError, match="separators"):
        parse_ref("a@b@c")


def test_every_rejection_quotes_the_expected_form() -> None:
    """An unusable grammar error is barely better than a traceback."""
    for token in ("", "grafana", "@1.2.3", "a@b@c", "grafana@"):
        with pytest.raises(ChartRefError) as caught:
            parse_ref(token)
        assert "CHART@VERSION" in str(caught.value)


def test_the_error_is_a_domain_error() -> None:
    """A non-CLI surface must be able to catch this without importing Typer."""
    assert issubclass(ChartRefError, ChartManagerError)


# --------------------------------------------------------------------------
# the invariant belongs to the type, not to one constructor
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "version"),
    [
        ("", "1.2.3"),
        ("grafana", ""),
        ("graf ana", "1.2.3"),
        ("grafana", "1.2 .3"),
        ("graf@ana", "1.2.3"),
        ("grafana", "1@2"),
    ],
)
def test_direct_construction_enforces_the_same_component_rules(
    name: str, version: str
) -> None:
    """Otherwise the flag form -- or P1b's reader -- could bypass the grammar."""
    with pytest.raises(ChartRefError):
        ChartRef(name=name, version=version)


def test_ref_from_parts_normalises_the_way_parse_ref_does() -> None:
    """The two entry points must not come to disagree about a component."""
    assert ref_from_parts("  grafana  ", "  1.2.3  ") == parse_ref("grafana@1.2.3")


def test_ref_from_parts_rejects_an_embedded_separator() -> None:
    """`--chart 'a@b'` must not smuggle a second separator past the grammar."""
    with pytest.raises(ChartRefError):
        ref_from_parts("a@b", "1.2.3")


def test_a_ref_is_hashable_and_compares_by_value() -> None:
    """Frozen: it is an identity, and the read side wants it as a dict key."""
    assert parse_ref("grafana@1.2.3") == parse_ref("grafana@1.2.3")
    assert len({parse_ref("grafana@1.2.3"), ref_from_parts("grafana", "1.2.3")}) == 1


# --------------------------------------------------------------------------
# the read side's CHART[@VERSION] selector (design doc 7.5)
# --------------------------------------------------------------------------


def test_a_bare_chart_is_a_selector_for_the_whole_history() -> None:
    selector = parse_selector("grafana")

    assert (selector.name, selector.version) == ("grafana", None)
    assert selector.correlation_id is None


def test_a_versioned_selector_narrows_to_one_release() -> None:
    selector = parse_selector("grafana@1.2.3")

    assert (selector.name, selector.version) == ("grafana", "1.2.3")


def test_the_selector_correlation_id_is_the_ref_wire_form() -> None:
    """Composed through `ChartRef`, so a selector cannot disagree with the
    join key the writer composes."""
    assert parse_selector("grafana@1.2.3").correlation_id == str(parse_ref("grafana@1.2.3"))


@pytest.mark.parametrize(
    "token",
    ["", "   ", "@1.2.3", "a@b@c", "grafana@", "graf ana", "graf ana@1.2.3"],
    ids=[
        "empty",
        "blank",
        "no-chart",
        "two-separators",
        "empty-version",
        "whitespace-in-name",
        "whitespace-in-versioned-name",
    ],
)
def test_selector_rejections_are_shared_with_the_ref_grammar(token: str) -> None:
    """Everything but the bare-chart form is rejected exactly as `parse_ref`
    rejects it: `grafana@` is malformed, only `grafana` is versionless."""
    with pytest.raises(ChartRefError):
        parse_selector(token)


def test_selector_whitespace_is_stripped_like_the_ref() -> None:
    assert parse_selector("  grafana  ") == ChartSelector(name="grafana")
    assert parse_selector(" grafana @ 1.2.3 ") == ChartSelector(name="grafana", version="1.2.3")


def test_direct_selector_construction_enforces_the_component_rules() -> None:
    """The invariant belongs to the type, exactly as it does for `ChartRef`."""
    with pytest.raises(ChartRefError):
        ChartSelector(name="graf@ana")
    with pytest.raises(ChartRefError):
        ChartSelector(name="grafana", version="1.2 .3")


def test_parse_ref_and_parse_selector_agree_on_the_versioned_form() -> None:
    """One grammar, two readings: the versioned selector and the ref carry
    the same halves."""
    ref = parse_ref("grafana@1.2.3")
    selector = parse_selector("grafana@1.2.3")

    assert (ref.name, ref.version) == (selector.name, selector.version)
