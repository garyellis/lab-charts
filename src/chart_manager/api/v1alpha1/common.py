"""Shared authored API vocabulary."""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict

ApiVersion = Literal["chartmanager.io/v1alpha1"]
API_VERSION: ApiVersion = get_args(ApiVersion)[0]


class ApiModel(BaseModel):
    """Authored model that rejects unknown keys and coerces known ones."""

    model_config = ConfigDict(extra="forbid")


class StrictApiModel(ApiModel):
    """Authored model that also rejects values of the wrong type."""

    model_config = ConfigDict(strict=True)
