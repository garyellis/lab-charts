"""A command's result as its document: plain data that json and yaml can encode."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def to_document(value: object) -> Any:
    """Return `value` as JSON-shaped data; keys are the field names.

    A dataclass contributes its stored fields, never its properties. A field
    with `metadata={"wire": False}` is left out, e.g. one holding credentials.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: to_document(getattr(value, item.name))
            for item in dataclasses.fields(value)
            if item.metadata.get("wire", True)
        }
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: to_document(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_document(item) for item in value]
    return value
