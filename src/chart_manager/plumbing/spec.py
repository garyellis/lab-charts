"""Per-chart `test-spec.yaml` schema + loader (pydantic at the IO boundary)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from chart_manager.plumbing.errors import SpecError


def ensure_chart_relative(values: list[str], *, label: str = "path") -> list[str]:
    """Reject absolute or parent-escaping paths declared in a chart spec.

    Shared by test-spec.yaml and validate-spec.yaml. Both resolve their
    entries against the chart directory, and `chart_dir / "/etc/passwd"` is
    `/etc/passwd` while `../..` walks out of the tree entirely -- so the
    same rule has to hold at both IO boundaries, not just the one that
    happened to implement it.
    """
    for value in values:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"{label} must be chart-relative: {value}")
    return values


class ChartRef(BaseModel):
    """Reference to another chart's profile (used by requires/reverseTests)."""

    model_config = ConfigDict(extra="forbid")

    chart: str
    profile: str = "minimal"


class CheckSpec(BaseModel):
    """A named post-install check declared by a profile."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = "helm-test"
    description: str | None = None


class ProfileSpec(BaseModel):
    """How to install and test a chart under one named profile."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    description: str | None = None
    namespace: str | None = None
    requires: list[ChartRef] = Field(default_factory=list)
    values: list[str] = Field(default_factory=lambda: ["values.yaml"])
    helm_test: bool = Field(default=True, alias="helmTest")
    checks: list[CheckSpec] = Field(default_factory=list)
    timeout: str = "10m"

    def effective_checks(self) -> list[CheckSpec]:
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
                CheckSpec(
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
        return ensure_chart_relative(values, label="value file")


class TestSpec(BaseModel):
    """Root of a chart's test-spec.yaml."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: int = 1
    profiles: dict[str, ProfileSpec]
    reverse_tests: list[ChartRef] = Field(default_factory=list, alias="reverseTests")

    def profile(self, name: str) -> ProfileSpec:
        """Look up a profile by name; SpecError lists available names."""
        try:
            return self.profiles[name]
        except KeyError as exc:
            profiles = ", ".join(sorted(self.profiles))
            raise SpecError(f"unknown profile '{name}'. available profiles: {profiles}") from exc


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Read a YAML file that must contain a mapping; empty file -> {}."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except OSError as exc:
        raise SpecError(f"failed to read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"{path} must contain a YAML mapping")
    return data


def load_test_spec(path: Path) -> TestSpec:
    """Load and validate a test-spec.yaml, wrapping failures in SpecError."""
    if not path.exists():
        raise SpecError(f"missing test spec: {path}")
    try:
        return TestSpec.model_validate(load_yaml_file(path))
    except ValueError as exc:
        raise SpecError(f"invalid test spec {path}: {exc}") from exc
