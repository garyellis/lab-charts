"""Authored live-cluster test configuration."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.paths import ensure_relative


class ClusterTestRef(BaseModel):
    """Reference to another chart's cluster-test profile."""

    model_config = ConfigDict(extra="forbid")

    chart: str
    profile: str = "minimal"


class ClusterTestProfile(BaseModel):
    """How to install and test a chart under one named profile."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    namespace: str | None = None
    requires: list[ClusterTestRef] = Field(default_factory=list)
    values: list[str] = Field(default_factory=lambda: ["values.yaml"])
    helm_test: bool = Field(default=True, alias="helmTest")
    timeout: str = "10m"

    @field_validator("values")
    @classmethod
    def values_must_be_relative(cls, values: list[str]) -> list[str]:
        """Reject absolute or parent-escaping values paths."""
        return ensure_relative(values, label="value file", relation="chart-relative")


class ClusterTestSpec(BaseModel):
    """Authored configuration for a chart's live-cluster test workflows."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    profiles: dict[str, ClusterTestProfile]
    dependent_tests: list[ClusterTestRef] = Field(
        default_factory=list,
        alias="dependentTests",
    )

    def profile(self, name: str) -> ClusterTestProfile:
        """Look up a profile by name; SpecError lists available names."""
        try:
            return self.profiles[name]
        except KeyError as exc:
            profiles = ", ".join(sorted(self.profiles))
            raise SpecError(f"unknown profile '{name}'. available profiles: {profiles}") from exc
