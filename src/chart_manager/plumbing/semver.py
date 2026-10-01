"""Strict SemVer 2.0 parsing, formatting and precedence.

One grammar for every version this package validates or compares: ASCII
only, no leading zeros in the numeric core or in numeric prerelease
identifiers (build metadata may have them, e.g. `+001`). Invalid input
raises ValueError; callers translate it into their own error type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NUMBER = r"0|[1-9][0-9]*"
_PRERELEASE_ID = rf"(?:{_NUMBER}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_BUILD_ID = r"[0-9A-Za-z-]+"
_SEMVER = re.compile(
    rf"({_NUMBER})\.({_NUMBER})\.({_NUMBER})"
    rf"(?:-({_PRERELEASE_ID}(?:\.{_PRERELEASE_ID})*))?"
    rf"(?:\+({_BUILD_ID}(?:\.{_BUILD_ID})*))?"
)


@dataclass(frozen=True)
class SemVer:
    """A parsed version; `str()` formats it back to its canonical spelling."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += "-" + ".".join(self.prerelease)
        if self.build:
            text += "+" + ".".join(self.build)
        return text

    @property
    def precedence(self) -> tuple[object, ...]:
        """Sort key for SemVer precedence; build metadata is ignored.

        A release sorts above its prereleases, and numeric identifiers sort
        numerically and below alphanumeric ones.
        """
        if not self.prerelease:
            return (self.major, self.minor, self.patch, 1, ())
        identifiers = tuple((0, int(p)) if p.isdigit() else (1, p) for p in self.prerelease)
        return (self.major, self.minor, self.patch, 0, identifiers)


def parse_semver(value: object) -> SemVer:
    """Parse a full SemVer 2.0 version, with optional prerelease and build metadata."""
    match = _SEMVER.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"invalid SemVer version: {value!r}")
    major, minor, patch, prerelease, build = match.groups()
    return SemVer(
        int(major),
        int(minor),
        int(patch),
        tuple(prerelease.split(".")) if prerelease else (),
        tuple(build.split(".")) if build else (),
    )


def parse_bare_version(value: object) -> SemVer:
    """Parse a bare `x.y.z` version: no prerelease, no build metadata."""
    version = parse_semver(value)
    if version.prerelease or version.build:
        raise ValueError(f"not a bare x.y.z version: {value!r}")
    return version
