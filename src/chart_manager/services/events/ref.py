"""`CHART@VERSION` -- the one grammar for naming a chart release.

The token is not a CLI convenience. It is already the events wire format:
`PlatformLifecycleEvent.correlation_id` is `f"{chart_name}@{chart_version}"`
(`lifecycle.py`), the join key that makes one version's timeline a timeline.
A surface that accepted `--chart` and `--version` separately would be
splitting a token the system composes anyway, and would then own the rule for
how the halves go back together -- in `cli/`, where a REST handler or a Slack
listener could not reach it.

So the grammar lives here, and the surface passes through what the user typed
(design commitment 6: *the surface never derives a request field from a
heuristic -- it passes what the user typed to a service resolver*).
`cli/events.py` calls `parse_ref` and `ref_from_parts`; nothing in `cli/`
looks for an `@`.

The rules, and why each is a *domain* rule rather than a CLI rule
-----------------------------------------------------------------

1. **Exactly one `@`.** A Helm chart name is an RFC 1123 label (lowercase
   alphanumerics and `-`) and a SemVer version admits only alphanumerics,
   `.`, `-` and `+`. Neither may contain `@`, so `a@b@c` is not an ambiguous
   split to be resolved by picking the first or last separator -- there is no
   reading of it under which some component is legitimate. It is malformed
   input, and saying so beats silently choosing a partition key.

2. **The version is required.** `chart_version` is nullable in the schema
   (None while a PR is open and nothing is published yet), but a ref with no
   version composes `correlation_id = "grafana@None"` -- a join key that
   joins nothing, written into a real ledger. Anything *emitting* an event
   knows the version it is reporting on, and the flag this replaces
   (`--version`) was already required, so this is not a new restriction.

3. **The chart is required.** Design doc 7.5, kept from the scripts: *version
   without chart is an error*. `store.py` partitions on `chart_name`, so a
   bare version is a cross-partition suffix scan -- the worst query in the
   system. Rejecting it here means no surface can express it by accident.

4. **Whitespace around the token, and around either component, is stripped;
   whitespace inside a component is rejected.** CI hands these values through
   shell variables, where a stray space is a transport artefact rather than
   something the caller meant. A space in the middle of a name is not.

`ChartRef` validates in `__post_init__` rather than trusting its two
constructors, so the invariant belongs to the type: a future reader side
(design doc P1b) that builds a ref some third way cannot skip the rules.

Not built here, deliberately: design doc 7.5 also gives `event list` a
`CHART[@VERSION]` form where the version is optional. That belongs to P1b
along with the command that needs it; adding the option now would be an
unused branch with no call site to keep it honest.
"""

from __future__ import annotations

from dataclasses import dataclass

from chart_manager.plumbing.errors import ChartManagerError

__all__ = ["SEPARATOR", "ChartRef", "ChartRefError", "parse_ref", "ref_from_parts"]

#: The one character that joins the two halves. Named so the schema comment
#: in `lifecycle.py`, this grammar and any future formatter cannot drift.
SEPARATOR = "@"

#: Quoted in every rejection, because a grammar error the caller cannot act
#: on is only marginally better than a traceback.
_EXPECTED = f"expected CHART{SEPARATOR}VERSION, for example 'grafana{SEPARATOR}1.2.3'"


class ChartRefError(ChartManagerError):
    """Raised when a chart ref does not parse.

    A `ChartManagerError` so a non-CLI surface gets a domain failure it can
    map to its own status code. `cli/events.py` narrows it to
    `typer.BadParameter` (exit 2, usage), matching how `_parse_at` already
    reports a malformed `--at` on the same commands.
    """


@dataclass(frozen=True, slots=True)
class ChartRef:
    """One chart at one version -- the addressable unit of the event ledger.

    `str(ref)` is exactly the `correlation_id` the writer composes, which is
    the point: the ref and the join key are the same token, not two
    representations that have to be kept in agreement.
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


def parse_ref(text: str) -> ChartRef:
    """Parse a `CHART@VERSION` token.

    Raises `ChartRefError` when the token is empty, carries no `@`, carries
    more than one, or has an empty half. See the module docstring for why
    each of those is rejected rather than guessed at.
    """
    token = text.strip()
    if not token:
        raise ChartRefError(f"a chart ref may not be empty; {_EXPECTED}")

    parts = token.split(SEPARATOR)
    if len(parts) == 1:
        raise ChartRefError(
            f"{token!r} has no version; {_EXPECTED}. Emitting an event always "
            "knows the version it reports on."
        )
    if len(parts) > 2:
        raise ChartRefError(
            f"{token!r} has {len(parts) - 1} {SEPARATOR!r} separators; {_EXPECTED}. "
            f"Neither a chart name nor a version may contain {SEPARATOR!r}."
        )

    name, version = (part.strip() for part in parts)
    if not name:
        # Called out separately from the generic empty-component message
        # because the reason is specific and worth telling the caller: see
        # rule 3.
        raise ChartRefError(
            f"{token!r} names a version with no chart; {_EXPECTED}. A version "
            "alone would have to be scanned for across every chart."
        )
    return ChartRef(name=name, version=version)


def ref_from_parts(name: str, version: str) -> ChartRef:
    """Build a ref from two already-separated halves.

    The entry point for the deprecated `--chart` / `--chart-version` flag
    pair. It normalises the same way `parse_ref` does -- rather than the
    surface calling `.strip()` and `ChartRef(...)` itself -- so the flag form
    and the positional form cannot come to disagree about what a component
    may be.
    """
    return ChartRef(name=name.strip(), version=version.strip())


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
