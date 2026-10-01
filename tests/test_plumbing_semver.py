"""Coverage for `plumbing/semver.py`: strict parsing, formatting and precedence."""

from __future__ import annotations

import pytest

from chart_manager.plumbing.semver import SemVer, parse_bare_version, parse_semver


@pytest.mark.parametrize(
    "raw", ["0.0.0", "1.2.3", "10.20.30", "1.0.0-0.alpha-1.0a", "1.0.0-rc.1+build.001", "1.0.0+001"]
)
def test_parse_semver_round_trips(raw: str) -> None:
    assert str(parse_semver(raw)) == raw


def test_parse_semver_splits_identifiers() -> None:
    assert parse_semver("1.2.3-rc.1+b.7") == SemVer(1, 2, 3, ("rc", "1"), ("b", "7"))


@pytest.mark.parametrize(
    "raw",
    [
        *("01.2.3", "1.02.3", "1.2.03", "1.0.0-01", "1.0.0-rc.01"),  # leading zeros
        *("1.2", "v1.2.3", "1.2.3-", "1.2.3+", "1.2.3-rc..1", "1.2.3+b_1", "1.2.3\n"),
        *("\u0661.2.3", "latest", "", None, 123),
    ],
)
def test_parse_semver_rejects_invalid_input(raw: object) -> None:
    with pytest.raises(ValueError, match="invalid SemVer version"):
        parse_semver(raw)


@pytest.mark.parametrize("raw", ["1.2.3-rc.1", "1.2.3+build"])
def test_parse_bare_version_rejects_prerelease_and_build(raw: str) -> None:
    assert parse_semver(raw)
    with pytest.raises(ValueError, match=r"bare x\.y\.z"):
        parse_bare_version(raw)


def test_precedence_follows_the_semver_specification() -> None:
    ordered = [
        *("1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2"),
        *("1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0-1", "2.0.0"),
    ]
    keys = [parse_semver(raw).precedence for raw in ordered]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


@pytest.mark.parametrize(
    ("lower", "higher"),
    [
        ("1.0.0-2", "1.0.0-10"),  # numeric identifiers compare numerically
        ("1.0.0-B", "1.0.0-a"),  # alphanumeric identifiers compare in ASCII order
        ("1.0.0-999", "1.0.0-a"),  # numeric sorts below alphanumeric
        ("1.0.0-pr.1", "1.0.0-pr.2"),
        ("1.0.0-1", "1.0.0"),  # a release sorts above its prereleases
    ],
)
def test_precedence_pairs(lower: str, higher: str) -> None:
    assert parse_semver(lower).precedence < parse_semver(higher).precedence


def test_precedence_ignores_build_metadata() -> None:
    assert parse_semver("1.0.0+build.2").precedence == parse_semver("1.0.0+build.1").precedence
