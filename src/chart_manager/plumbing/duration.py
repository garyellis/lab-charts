from __future__ import annotations

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
