"""Shared authored API vocabulary."""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, field_validator

from chart_manager.plumbing.names import dns_label

ApiVersion = Literal["chartmanager.io/v1alpha1"]
API_VERSION: ApiVersion = get_args(ApiVersion)[0]


class ApiModel(BaseModel):
    """Authored model that rejects unknown keys and coerces known ones."""

    model_config = ConfigDict(extra="forbid")


class StrictApiModel(ApiModel):
    """Authored model that also rejects values of the wrong type."""

    model_config = ConfigDict(strict=True)


class ResourceMetadata(StrictApiModel):
    """Identity shared by both local kinds.

    `name` is a lowercase DNS label, which is stricter than
    `ChartLifecycleMetadata.name` in the lifecycle group -- that one accepts any
    non-padded string. The two are separate models on purpose; merging them
    would change what one of the groups accepts.
    """

    name: str

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return dns_label(value, field="metadata.name")
