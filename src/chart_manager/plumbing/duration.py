"""Parse kube-style duration strings into seconds."""

from __future__ import annotations

import math

from chart_manager.plumbing.errors import ChartManagerError

_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(value: str) -> float:
    """Parse a kube-style duration ("60s", "5m", "1h") into seconds.

    Intentionally narrow: we only accept the units kubectl itself uses for
    --timeout. Bare integers are treated as seconds. Invalid input raises
    ChartManagerError so the CLI's top-level handler surfaces it cleanly
    (a raw ValueError would tracebacks-out instead).
    """
    raw = value
    value = value.strip()
    if not value:
        raise ChartManagerError(f"invalid duration: {raw!r} (empty)")
    try:
        if value[-1] in _DURATION_UNITS:
            return float(value[:-1]) * _DURATION_UNITS[value[-1]]
        return float(value)
    except ValueError as exc:
        raise ChartManagerError(f"invalid duration: {raw!r} ({exc})") from exc


def require_positive_seconds(name: str, value: float) -> None:
    """Raise ChartManagerError unless `value` is a positive, finite number of seconds.

    The numeric counterpart of `parse_duration` for callers that already
    hold seconds (e.g. a request dataclass built from Python rather than the
    CLI). NaN needs the explicit `isfinite` check: every ordering comparison
    against NaN is False, so a bare `< minimum` guard would wave it through.
    `bool` is rejected even though it is an `int`, and a string gets a
    ChartManagerError naming the field rather than a TypeError from `isfinite`.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ChartManagerError(f"{name} must be a number of seconds (got {value!r})")
    if not math.isfinite(value) or value <= 0:
        raise ChartManagerError(f"{name} must be positive and finite (got {value!r})")
