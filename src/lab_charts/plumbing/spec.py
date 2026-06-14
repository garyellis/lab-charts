from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lab_charts.plumbing.errors import SpecError


class ChartRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chart: str
    profile: str = "minimal"


class CheckSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = "helm-test"
    description: str | None = None


class ProfileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    description: str | None = None
    namespace: str | None = None
    requires: list[ChartRef] = Field(default_factory=list)
    values: list[str] = Field(default_factory=lambda: ["values.yaml"])
    helm_test: bool = Field(default=True, alias="helmTest")
    checks: list[CheckSpec] = Field(default_factory=list)
    timeout: str = "10m"

    @field_validator("values")
    @classmethod
    def values_must_be_relative(cls, values: list[str]) -> list[str]:
        for value in values:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"value file must be chart-relative: {value}")
        return values


class TestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: int = 1
    profiles: dict[str, ProfileSpec]
    reverse_tests: list[ChartRef] = Field(default_factory=list, alias="reverseTests")

    def profile(self, name: str) -> ProfileSpec:
        try:
            return self.profiles[name]
        except KeyError as exc:
            profiles = ", ".join(sorted(self.profiles))
            raise SpecError(f"unknown profile '{name}'. available profiles: {profiles}") from exc


def load_yaml_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except OSError as exc:
        raise SpecError(f"failed to read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"{path} must contain a YAML mapping")
    return data


def load_test_spec(path: Path) -> TestSpec:
    if not path.exists():
        raise SpecError(f"missing test spec: {path}")
    try:
        return TestSpec.model_validate(load_yaml_file(path))
    except ValueError as exc:
        raise SpecError(f"invalid test spec {path}: {exc}") from exc
