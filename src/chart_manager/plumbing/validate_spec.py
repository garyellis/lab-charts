"""Per-chart `validate-spec.yaml` schema + loader.

Mirrors `plumbing/spec.py` style: pydantic at the IO boundary, `SpecError`
on any parse/shape failure. `services/validate/worklist.py` is the only
consumer: it loads each chart's spec and turns it into the (chart, env)
rows a run fans out over, along with the chart path, values files and
policy dirs each row runs with. Everything downstream sees `RowConfig`,
not this model.

`version: 1` is the only accepted envelope. Bumping the major signals a
breaking shape change; minor additions stay additive within the major.
Unknown majors fail loudly per plan decision #5.
"""
from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.spec import ensure_chart_relative, load_yaml_file

# Literal string used as a trigger value to opt into basename-derived env
# fanout (e.g. envs/dev.yaml -> dev). Kept as a constant so the worklist
# layer and the spec validator agree on the spelling.
MATCH_BY_BASENAME = "match-by-basename"

TriggerValue = list[str] | Literal["match-by-basename"]


class EnvironmentSpec(BaseModel):
    """Per-environment overrides: namespace and extra values files."""

    model_config = ConfigDict(extra="forbid")

    namespace: str | None = None
    values: list[str] = Field(default_factory=list)

    @field_validator("values")
    @classmethod
    def values_must_be_relative(cls, values: list[str]) -> list[str]:
        """Reject absolute or parent-escaping values paths (as ProfileSpec does)."""
        return ensure_chart_relative(values, label="value file")


class PoliciesSpec(BaseModel):
    """Extra policy paths to run beyond the repo-wide defaults."""

    model_config = ConfigDict(extra="forbid")

    extra: list[str] = Field(default_factory=list)

    @field_validator("extra")
    @classmethod
    def extra_must_be_relative(cls, extra: list[str]) -> list[str]:
        """Reject absolute or parent-escaping policy paths."""
        return ensure_chart_relative(extra, label="policy path")


class ValidateSpec(BaseModel):
    """Root of a chart's validate-spec.yaml."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: int = 1
    release_name: str
    namespace_template: str | None = None

    helm_version: str | None = None
    helm_bin: str | None = None

    kubernetes_version: str | None = None
    schema_locations: list[str] = Field(default_factory=list)

    environments: dict[str, EnvironmentSpec]
    triggers: dict[str, TriggerValue] = Field(default_factory=dict)
    # Opt-in safety net: when true, an edited chart file that matches no
    # trigger fans out to every env in `environments` (instead of being
    # silently dropped). Catches under-enumerated triggers — e.g. a chart
    # whose author wrote a trigger for `values.yaml` but forgot `templates/`.
    triggers_strict: bool = False
    policies: PoliciesSpec = Field(default_factory=PoliciesSpec)

    skip: bool = False

    @model_validator(mode="after")
    def _check_version(self) -> ValidateSpec:
        """Reject any version envelope other than 1."""
        if self.version != 1:
            raise ValueError(
                f"unsupported validate-spec version: {self.version} (only version 1 is supported)"
            )
        return self

    @model_validator(mode="after")
    def _check_helm_exclusive(self) -> ValidateSpec:
        """Forbid setting both helm_version and helm_bin."""
        if self.helm_version is not None and self.helm_bin is not None:
            raise ValueError("helm_version and helm_bin are mutually exclusive")
        return self

    @model_validator(mode="after")
    def _check_environments(self) -> ValidateSpec:
        """Require >=1 environment; each needs a namespace unless a template exists."""
        if not self.environments:
            raise ValueError("environments must declare at least one entry")
        if self.namespace_template is None:
            missing = [name for name, env in self.environments.items() if not env.namespace]
            if missing:
                raise ValueError(
                    "namespace_template is unset; every environment must declare 'namespace'. "
                    f"missing: {', '.join(sorted(missing))}"
                )
        return self

    @model_validator(mode="after")
    def _check_triggers(self) -> ValidateSpec:
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


def resolve_namespace(spec: ValidateSpec, env: str) -> str:
    """Return the namespace for `env`, preferring explicit per-env value.

    Falls back to `${env}` substitution against `spec.namespace_template`.
    Model validators guarantee at least one of the two is present.
    """
    try:
        env_spec = spec.environments[env]
    except KeyError as exc:
        raise SpecError(f"unknown environment '{env}' in validate-spec") from exc
    if env_spec.namespace:
        return env_spec.namespace
    if spec.namespace_template is None:
        # Defended by validator, but be explicit so a misuse surfaces here.
        raise SpecError(
            f"cannot resolve namespace for env '{env}': "
            "no explicit namespace and no namespace_template"
        )
    return Template(spec.namespace_template).safe_substitute(env=env)


def load_validate_spec(path: Path) -> ValidateSpec:
    """Load and validate a validate-spec.yaml, wrapping failures in SpecError."""
    if not path.exists():
        raise SpecError(f"missing validate spec: {path}")
    try:
        return ValidateSpec.model_validate(load_yaml_file(path))
    except ValueError as exc:
        raise SpecError(f"invalid validate spec {path}: {exc}") from exc
