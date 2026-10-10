"""Parse kube-style duration strings into seconds, validate seconds, and render them.

One rule for every timeout this package handles: a duration is a positive,
finite number of seconds. `parse_duration` applies it to operator strings,
`require_positive_seconds` to values that are already numeric.
`format_duration` renders seconds for a `--timeout` flag.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal

from chart_manager.plumbing.errors import ChartManagerError

_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
# "ms" comes first so "500ms" is not read as "500m" followed by "s".
_PART = re.compile(rf"({_NUMBER})(ms|s|m|h)")
_DURATION = re.compile(rf"(?:{_NUMBER}(?:ms|s|m|h))+")


def parse_duration(value: str) -> float:
    """Parse a Go-style duration ("60s", "5m", "1h30m", "500ms") into seconds.

    Accepts one or more `<number><unit>` pairs with units ms, s, m and h,
    as helm and kubectl do for --timeout. Bare numbers are treated as
    seconds. The result must also be positive and finite: `float()` happily
    accepts "nan", "inf" and "-5", none of which is a usable timeout (NaN
    defeats every deadline comparison, inf never expires, zero or negative
    expires at once).
    Invalid input raises ChartManagerError so the CLI's top-level handler
    surfaces it cleanly (a raw ValueError would tracebacks-out instead).
    """
    raw = value
    value = value.strip()
    if not value:
        raise ChartManagerError(f"invalid duration: {raw!r} (empty)")
    if _DURATION.fullmatch(value):
        seconds = sum(float(n) * _DURATION_UNITS[u] for n, u in _PART.findall(value))
    else:
        try:
            seconds = float(value)
        except ValueError as exc:
            raise ChartManagerError(f"invalid duration: {raw!r} ({exc})") from exc
    try:
        require_positive_seconds("duration", seconds)
    except ChartManagerError as exc:
        raise ChartManagerError(f"invalid duration: {raw!r} ({exc})") from exc
    return seconds


def require_positive_seconds(name: str, value: float) -> None:
    """Raise ChartManagerError unless `value` is a positive, finite number of seconds.

    The single home of the positive-and-finite rule: `parse_duration`
    applies it to every parsed string, and callers that already hold seconds
    (e.g. a request dataclass built from Python rather than the CLI) call it
    directly. NaN needs the explicit `isfinite` check: every ordering comparison
    against NaN is False, so a bare `< minimum` guard would wave it through.
    `bool` is rejected even though it is an `int`, and a string gets a
    ChartManagerError naming the field rather than a TypeError from `isfinite`.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ChartManagerError(f"{name} must be a number of seconds (got {value!r})")
    if not math.isfinite(value) or value <= 0:
        raise ChartManagerError(f"{name} must be positive and finite (got {value!r})")


def format_duration(seconds: float) -> str:
    """Render seconds as a Go duration string for helm's and kubectl's `--timeout` flag.

    Plain decimal seconds only, fractional part preserved: 300.0 -> "300s",
    1.5 -> "1.5s", 1e-05 -> "0.00001s". Going through `Decimal(repr(...))`
    keeps the shortest round-tripping digits and avoids both the "1e-05s"
    exponent form (Go's `time.ParseDuration` rejects it) and a noisy
    trailing ".0". Negative and non-finite values are programming errors:
    request validation rejects them long before a subprocess is built.
    """
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"duration must be finite and >= 0 (got {seconds!r})")
    return f"{Decimal(repr(float(seconds))).normalize():f}s"
