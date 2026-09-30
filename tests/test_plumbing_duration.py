"""Coverage for `plumbing/duration.py`: string parsing and numeric validation."""
from __future__ import annotations

import pytest

from chart_manager.plumbing.duration import parse_duration, require_positive_seconds
from chart_manager.plumbing.errors import ChartManagerError


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [("10s", 10.0), ("5m", 300.0), ("1h", 3600.0), ("90", 90.0), ("1.5s", 1.5)],
)
def test_parse_duration_units(raw: str, seconds: float) -> None:
    assert parse_duration(raw) == seconds


@pytest.mark.parametrize("raw", ["", "  ", "5 mins", "abc", "5d"])
def test_parse_duration_rejects_malformed_input(raw: str) -> None:
    with pytest.raises(ChartManagerError, match="invalid duration"):
        parse_duration(raw)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "0", "0s", "-5m"])
def test_parse_duration_rejects_non_positive_or_non_finite(raw: str) -> None:
    # Each of these is a valid float to Python, so only the explicit
    # positive-and-finite rule stops it reaching a deadline computation.
    with pytest.raises(ChartManagerError, match=f"invalid duration: {raw!r}"):
        parse_duration(raw)


@pytest.mark.parametrize("value", [0.001, 1, 30.0, 3600.0])
def test_require_positive_seconds_accepts_positive_finite(value: float) -> None:
    require_positive_seconds("budget", value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 0, 0.0, -1.0])
def test_require_positive_seconds_rejects_non_positive_or_non_finite(value: float) -> None:
    with pytest.raises(ChartManagerError, match="budget must be positive and finite"):
        require_positive_seconds("budget", value)


@pytest.mark.parametrize("value", ["5m", None, True, False])
def test_require_positive_seconds_rejects_non_numbers(value: object) -> None:
    # A caller still passing the old duration string gets a message naming
    # the field, not a TypeError from math.isfinite.
    with pytest.raises(ChartManagerError, match="budget must be a number of seconds"):
        require_positive_seconds("budget", value)  # type: ignore[arg-type]
