"""``lifecycle.chartmanager.io/v1alpha1`` -- the authored ``ChartLifecycle`` contract.

``chart-lifecycle.yaml`` is the only per-chart lifecycle document.  This
module owns its complete accepted shape: the envelope, the metadata identity,
and both capability sections (``spec.validation`` and ``spec.clusterTest``).

Everything here is decidable from one document.  Loading the file, agreeing
its ``metadata.name`` with the chart directory and ``Chart.yaml``, deciding
whether a capability is enabled, resolving namespaces, and looking a profile
up by name all need more than the document and therefore live in
``chart_manager.services``.

Types are declared dependency-first -- cluster test, then manifest
validation, then the envelope -- so the module reads bottom-up from the
leaves an author writes to the wrapper they write around them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from chart_manager.api.base import ApiModel, StrictApiModel
from chart_manager.plumbing.paths import ensure_relative

__all__ = [
    "LIFECYCLE_API_VERSION",
    "LIFECYCLE_KIND",
    "MATCH_BY_BASENAME",
    "ChartLifecycle",
    "ChartLifecycleMetadata",
    "ChartLifecycleSpec",
    "ClusterTestProfile",
    "ClusterTestRef",
    "ClusterTestSpec",
    "ManifestValidationEnvironmentSpec",
    "ManifestValidationPolicySpec",
    "ManifestValidationSpec",
    "ManifestValidationValidatorsSpec",
    "TriggerValue",
]

LIFECYCLE_API_VERSION = "lifecycle.chartmanager.io/v1alpha1"
LIFECYCLE_KIND = "ChartLifecycle"

# Literal string used as a trigger value to opt into basename-derived env
# fanout (e.g. envs/dev.yaml -> dev). Kept as a constant so the worklist
# layer and the spec validator agree on the spelling.
MATCH_BY_BASENAME = "match-by-basename"

TriggerValue = list[str] | Literal["match-by-basename"]


# ---------------------------------------------------------------------------
# spec.clusterTest -- authored live-cluster test configuration
# ---------------------------------------------------------------------------


class ClusterTestRef(ApiModel):
    """Reference to another chart's cluster-test profile."""

    chart: str
    profile: str = "minimal"


class ClusterTestProfile(ApiModel):
    """How to install and test a chart under one named profile."""

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


class ClusterTestSpec(ApiModel):
    """Authored configuration for a chart's live-cluster test workflows."""

    enabled: bool = True
    profiles: dict[str, ClusterTestProfile]
    dependent_tests: list[ClusterTestRef] = Field(
        default_factory=list,
        alias="dependentTests",
    )


# ---------------------------------------------------------------------------
# spec.validation -- authored manifest-validation configuration
# ---------------------------------------------------------------------------


class ManifestValidationEnvironmentSpec(ApiModel):
    """Per-environment overrides: namespace and extra values files."""

    namespace: str | None = None
    values: list[str] = Field(default_factory=list)

    @field_validator("values")
    @classmethod
    def values_must_be_relative(cls, values: list[str]) -> list[str]:
        """Reject escaping values paths, as cluster-test profiles do."""
        return ensure_relative(values, label="value file", relation="chart-relative")


class ManifestValidationPolicySpec(ApiModel):
    """Extra policy paths to run beyond the repo-wide defaults."""

    extra: list[str] = Field(default_factory=list)

    @field_validator("extra")
    @classmethod
    def extra_must_be_relative(cls, extra: list[str]) -> list[str]:
        """Reject absolute or parent-escaping policy paths."""
        return ensure_relative(extra, label="policy path", relation="chart-relative")


class ManifestValidationValidatorsSpec(ApiModel):
    """Per-validator gates for the rendered-manifest pipeline.

    Defaults preserve the historical pipeline for every existing lifecycle
    document.  The nested section gives future validators a stable home
    without adding more top-level validation flags.
    """

    kubeconform: bool = True
    policy: bool = True


class ManifestValidationSpec(ApiModel):
    """Authored configuration for a chart's manifest-validation pipeline."""

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
    validators: ManifestValidationValidatorsSpec = Field(
        default_factory=ManifestValidationValidatorsSpec
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


# ---------------------------------------------------------------------------
# The ChartLifecycle envelope
# ---------------------------------------------------------------------------


class ChartLifecycleMetadata(StrictApiModel):
    """Identity of the chart governed by a lifecycle document."""

    name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def name_must_be_exact(cls, name: str) -> str:
        """Reject whitespace-only and silently normalized chart names."""
        if name != name.strip():
            raise ValueError("metadata.name must not have leading or trailing whitespace")
        return name


class ChartLifecycleSpec(StrictApiModel):
    """Capabilities authored for one chart."""

    enabled: bool = True
    validation: ManifestValidationSpec | None = None
    cluster_test: ClusterTestSpec | None = Field(default=None, alias="clusterTest")


class ChartLifecycle(StrictApiModel):
    """Kubernetes-style lifecycle intent envelope for one Helm chart."""

    api_version: Literal["lifecycle.chartmanager.io/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["ChartLifecycle"]
    metadata: ChartLifecycleMetadata
    spec: ChartLifecycleSpec
