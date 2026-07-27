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


class ClusterCheckSpec(BaseModel):
    """A named post-install check declared by a profile."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = "helm-test"
    description: str | None = None


class ClusterTestProfile(BaseModel):
    """How to install and test a chart under one named profile."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    namespace: str | None = None
    requires: list[ClusterTestRef] = Field(default_factory=list)
    values: list[str] = Field(default_factory=lambda: ["values.yaml"])
    helm_test: bool = Field(default=True, alias="helmTest")
    checks: list[ClusterCheckSpec] = Field(default_factory=list)
    timeout: str = "10m"

    def effective_checks(self) -> list[ClusterCheckSpec]:
        """Every check this profile actually runs, declared or implicit.

        `helmTest: true` runs the chart's helm test hooks without the
        profile having to declare a check for it, so the check list a
        caller sees must include that implicit entry. An explicitly
        declared `helm-test` check wins -- the profile author gets to name
        and describe it -- so we only synthesize one when none is present.
        """
        checks = list(self.checks)
        if self.helm_test and not any(check.type == "helm-test" for check in checks):
            checks.append(
                ClusterCheckSpec(
                    name="helm-test",
                    type="helm-test",
                    description="Run Helm test hooks for the release.",
                )
            )
        return checks

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
