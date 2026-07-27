"""Authored manifest-validation capability configuration.

The model is composed beneath ``spec.validation`` in the standalone
``chart-lifecycle.yaml`` resource. Loading and envelope validation belong to
``services.chart_config``; runtime consumers resolve this authored model
into ``ResolvedManifestValidation`` before executing cases.
"""

from __future__ import annotations

from string import Template
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.paths import ensure_relative

# Literal string used as a trigger value to opt into basename-derived env
# fanout (e.g. envs/dev.yaml -> dev). Kept as a constant so the worklist
# layer and the spec validator agree on the spelling.
MATCH_BY_BASENAME = "match-by-basename"

TriggerValue = list[str] | Literal["match-by-basename"]


class ManifestValidationEnvironmentSpec(BaseModel):
    """Per-environment overrides: namespace and extra values files."""

    model_config = ConfigDict(extra="forbid")

    namespace: str | None = None
    values: list[str] = Field(default_factory=list)

    @field_validator("values")
    @classmethod
    def values_must_be_relative(cls, values: list[str]) -> list[str]:
        """Reject escaping values paths, as cluster-test profiles do."""
        return ensure_relative(values, label="value file", relation="chart-relative")


class ManifestValidationPolicySpec(BaseModel):
    """Extra policy paths to run beyond the repo-wide defaults."""

    model_config = ConfigDict(extra="forbid")

    extra: list[str] = Field(default_factory=list)

    @field_validator("extra")
    @classmethod
    def extra_must_be_relative(cls, extra: list[str]) -> list[str]:
        """Reject absolute or parent-escaping policy paths."""
        return ensure_relative(extra, label="policy path", relation="chart-relative")


class ManifestValidationSpec(BaseModel):
    """Authored configuration for a chart's manifest-validation pipeline."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    release_name: str = Field(alias="releaseName")
    namespace_template: str | None = Field(default=None, alias="namespaceTemplate")

    helm_version: str | None = Field(default=None, alias="helmVersion")
    helm_binary: str | None = Field(default=None, alias="helmBinary")

    kubernetes_version: str | None = Field(default=None, alias="kubernetesVersion")
    schema_locations: list[str] = Field(default_factory=list, alias="schemaLocations")

    environments: dict[str, ManifestValidationEnvironmentSpec]
    triggers: dict[str, TriggerValue] = Field(default_factory=dict)
    # Explicit exclusions for chart-relative files which intentionally do
    # not affect rendered output. Keeping this separate from `triggers`
    # preserves the version-1 trigger mapping unchanged while making a
    # deliberate exclusion distinguishable from an accidental coverage gap.
    trigger_ignores: list[str] = Field(default_factory=list, alias="triggerIgnores")
    # Safety policy for changed chart files which match no explicit trigger.
    # The default records a diagnostic without adding work; the stronger
    # policy validates every configured environment.
    unmatched_changes: Literal["warn", "all-environments"] = Field(
        default="warn",
        alias="unmatchedChanges",
    )
    policies: ManifestValidationPolicySpec = Field(default_factory=ManifestValidationPolicySpec)

    @model_validator(mode="after")
    def _check_helm_exclusive(self) -> ManifestValidationSpec:
        """Forbid setting both helmVersion and helmBinary."""
        if self.helm_version is not None and self.helm_binary is not None:
            raise ValueError("helmVersion and helmBinary are mutually exclusive")
        return self

    @model_validator(mode="after")
    def _check_environments(self) -> ManifestValidationSpec:
        """Require >=1 environment; each needs a namespace unless a template exists."""
        if not self.environments:
            raise ValueError("environments must declare at least one entry")
        if self.namespace_template is None:
            missing = [name for name, env in self.environments.items() if not env.namespace]
            if missing:
                raise ValueError(
                    "namespaceTemplate is unset; every environment must declare 'namespace'. "
                    f"missing: {', '.join(sorted(missing))}"
                )
        return self

    @model_validator(mode="after")
    def _check_triggers(self) -> ManifestValidationSpec:
        """Each trigger must be MATCH_BY_BASENAME or a list of known envs."""
        known = set(self.environments)
        for pattern, value in self.triggers.items():
            if isinstance(value, str):
                if value != MATCH_BY_BASENAME:
                    raise ValueError(
                        f"trigger '{pattern}' string value must be "
                        f"'{MATCH_BY_BASENAME}', got {value!r}"
                    )
                continue
            unknown = [env for env in value if env not in known]
            if unknown:
                raise ValueError(
                    f"trigger '{pattern}' references unknown environment(s): {', '.join(unknown)}"
                )
        return self

    @field_validator("trigger_ignores")
    @classmethod
    def trigger_ignores_must_be_relative(cls, patterns: list[str]) -> list[str]:
        """Reject ignore patterns that point outside the chart."""
        return ensure_relative(
            patterns,
            label="trigger ignore pattern",
            relation="chart-relative",
        )


def resolve_namespace(spec: ManifestValidationSpec, env: str) -> str:
    """Return the namespace for `env`, preferring explicit per-env value.

    Falls back to `${env}` substitution against `spec.namespace_template`.
    Model validators guarantee at least one of the two is present.
    """
    try:
        env_spec = spec.environments[env]
    except KeyError as exc:
        raise SpecError(f"unknown environment '{env}' in manifest validation") from exc
    if env_spec.namespace:
        return env_spec.namespace
    if spec.namespace_template is None:
        # Defended by validator, but be explicit so a misuse surfaces here.
        raise SpecError(
            f"cannot resolve namespace for env '{env}': "
            "no explicit namespace and no namespaceTemplate"
        )
    return Template(spec.namespace_template).safe_substitute(env=env)
