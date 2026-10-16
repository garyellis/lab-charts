"""`to_document`: a result's stored fields as plain JSON-shaped data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import BaseModel, Field

from chart_manager.plumbing.documents import to_document


class Colour(Enum):
    RED = "red"


class Model(BaseModel):
    chart_name: str = Field(alias="chartName")
    note: str | None = None


@dataclass(frozen=True)
class Result:
    path: Path
    tags: tuple[str, ...]
    secret: str = field(default="token", metadata={"wire": False})

    @property
    def ok(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Result(Path("a/b"), ("x", "y")), {"path": "a/b", "tags": ["x", "y"]}),
        (Model(chartName="alloy"), {"chartName": "alloy"}),
        (Colour.RED, "red"),
        (datetime(2026, 10, 8, 12, 0, tzinfo=UTC), "2026-10-08T12:00:00+00:00"),
        (MappingProxyType({"k": [Colour.RED, None]}), {"k": ["red", None]}),
    ],
)
def test_to_document(value: object, expected: object) -> None:
    assert to_document(value) == expected
