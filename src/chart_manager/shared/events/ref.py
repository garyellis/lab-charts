"""`CHART@VERSION` -- the one grammar for naming a chart release.

The token is the events wire format: `PlatformLifecycleEvent.correlation_id`
is `f"{chart_name}@{chart_version}"`. Surfaces pass what the user typed to
`parse_ref` or `parse_selector`.

Rules:

1. Exactly one `@`. Neither a Helm chart name nor a SemVer version may
   contain one, so `a@b@c` is malformed, not ambiguous.
2. The version is required by `parse_ref`; without one, the event's
   `correlation_id` would be `"grafana@None"`.
3. The chart is required. A bare version would be a scan across every
   `chart_name` partition.
4. Whitespace around the token or a component is stripped; whitespace inside
   a component is rejected.

The read side's form is `CHART[@VERSION]` (`ChartSelector`/`parse_selector`).
"""

from __future__ import annotations

from dataclasses import dataclass

from chart_manager.plumbing.errors import ChartManagerError

__all__ = [
    "SEPARATOR",
    "ChartRef",
    "ChartRefError",
    "ChartSelector",
    "parse_ref",
    "parse_selector",
]

#: The one character that joins the two halves.
SEPARATOR = "@"

#: Quoted in every rejection.
_EXPECTED = f"expected CHART{SEPARATOR}VERSION, for example 'grafana{SEPARATOR}1.2.3'"


class ChartRefError(ChartManagerError):
    """Raised when a chart ref does not parse."""


@dataclass(frozen=True, slots=True)
class ChartRef:
    """One chart at one version -- the addressable unit of the event ledger.

    `str(ref)` is exactly the `correlation_id` the writer composes.
    """

    name: str
    version: str

    def __post_init__(self) -> None:
        """Enforce rules 1, 3 and 4 on each component, however it was built."""
        _validate("chart name", self.name)
        _validate("chart version", self.version)

    def __str__(self) -> str:
        """Render the ref, i.e. the event `correlation_id`."""
        return f"{self.name}{SEPARATOR}{self.version}"


@dataclass(frozen=True, slots=True)
class ChartSelector:
    """`CHART[@VERSION]` -- the read side's optional-version selection.

    A bare chart selects a whole history; a versioned one selects a single
    release timeline.
    """

    name: str
    version: str | None = None

    def __post_init__(self) -> None:
        """Hold each present component to the same rules a ref enforces."""
        _validate("chart name", self.name)
        if self.version is not None:
            _validate("chart version", self.version)

    @property
    def correlation_id(self) -> str | None:
        """The join key this selector narrows to; None selects the whole chart."""
        if self.version is None:
            return None
        return str(ChartRef(name=self.name, version=self.version))


def parse_selector(text: str) -> ChartSelector:
    """Parse a `CHART[@VERSION]` token -- the read side's form.

    Accepts everything `parse_ref` accepts plus a bare chart name.
    """
    token = text.strip()
    if not token:
        raise ChartRefError(f"a chart ref may not be empty; {_EXPECTED}")

    parts = token.split(SEPARATOR)
    if len(parts) > 2:
        raise ChartRefError(
            f"{token!r} has {len(parts) - 1} {SEPARATOR!r} separators; {_EXPECTED}. "
            f"Neither a chart name nor a version may contain {SEPARATOR!r}."
        )
    if len(parts) == 1:
        return ChartSelector(name=token)

    name, version = (part.strip() for part in parts)
    if not name:
        # Rule 3, with its own message.
        raise ChartRefError(
            f"{token!r} names a version with no chart; {_EXPECTED}. A version "
            "alone would have to be scanned for across every chart."
        )
    return ChartSelector(name=name, version=version)


def parse_ref(text: str) -> ChartRef:
    """Parse a `CHART@VERSION` token -- the emit-side form, version mandatory.

    Raises `ChartRefError` when the token is empty, carries no `@`, carries
    more than one, or has an empty half.
    """
    selector = parse_selector(text)
    if selector.version is None:
        raise ChartRefError(
            f"{selector.name!r} has no version; {_EXPECTED}. Emitting an event "
            "always knows the version it reports on."
        )
    return ChartRef(name=selector.name, version=selector.version)


def _validate(label: str, value: str) -> None:
    """Reject a component that is empty, contains `@`, or contains whitespace."""
    if not value:
        raise ChartRefError(f"the {label} is empty; {_EXPECTED}")
    if SEPARATOR in value:
        raise ChartRefError(
            f"the {label} {value!r} contains {SEPARATOR!r}, which it may not; {_EXPECTED}"
        )
    if any(character.isspace() for character in value):
        raise ChartRefError(
            f"the {label} {value!r} contains whitespace, which it may not; {_EXPECTED}"
        )
