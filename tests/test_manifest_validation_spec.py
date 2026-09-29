"""Unit tests for the nested ``ManifestValidationSpec`` capability model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chart_manager.api.v1alpha1.chart_lifecycle import (
    ALL_ENVIRONMENTS,
    MATCH_BY_BASENAME,
    ManifestValidationSpec,
)
from chart_manager.plumbing.errors import SpecError
from chart_manager.services.manifest_validation.namespaces import resolve_namespace


def _spec(**overrides: object) -> ManifestValidationSpec:
    raw: dict[str, object] = {
        "releaseName": "demo",
        "namespaceTemplate": "lab-${env}",
        "environments": {"dev": {"values": ["values.yaml"]}},
    }
    raw.update(overrides)
    return ManifestValidationSpec.model_validate(raw)


def test_full_authored_shape_uses_camel_case() -> None:
    spec = ManifestValidationSpec.model_validate(
        {
            "enabled": True,
            "releaseName": "demo",
            "namespaceTemplate": "lab-${env}",
            "helmVersion": "4.1.3",
            "schemaLocations": ["schemas/custom.json"],
            "ignoreMissingSchemas": ["UnpublishedKind"],
            "environments": {
                "dev": {"values": ["values.yaml", "values-dev.yaml"]},
                "prod": {"namespace": "lab-prod", "values": ["values.yaml"]},
            },
            "triggers": {
                "values.yaml": ["dev", "prod"],
                "envs/*.yaml": MATCH_BY_BASENAME,
            },
            "triggerIgnores": ["README.md", "docs/**"],
            "unmatchedChanges": "all-environments",
            "validators": {"kubeconform": False, "policy": True},
            "policies": {"extra": ["extra/policies"]},
        }
    )

    assert spec.release_name == "demo"
    assert spec.helm_version == "4.1.3"
    assert spec.schema_locations == ["schemas/custom.json"]
    assert spec.ignore_missing_schemas == ["UnpublishedKind"]
    assert spec.unmatched_changes == "all-environments"
    assert spec.triggers["envs/*.yaml"] == MATCH_BY_BASENAME
    assert spec.trigger_ignores == ["README.md", "docs/**"]
    assert spec.policies.extra == ["extra/policies"]
    assert spec.validators.kubeconform is False
    assert spec.validators.policy is True


def test_validators_default_to_the_existing_full_pipeline() -> None:
    spec = _spec()

    assert spec.validators.kubeconform is True
    assert spec.validators.policy is True


def test_validators_reject_unknown_names() -> None:
    with pytest.raises(ValidationError):
        _spec(validators={"kubeconform": True, "conftest": False})


@pytest.mark.parametrize(
    "location",
    [
        "default",
        "https://schemas.example.test/{{.ResourceKind}}.json",
        "/tmp/schema.json",
        "../schemas/custom.json",
        "schemas/../custom.json",
        "schemas\\custom.json",
    ],
)
def test_schema_locations_are_additive_repository_local_paths(location: str) -> None:
    with pytest.raises(ValidationError, match="schema location"):
        _spec(schemaLocations=[location])


def test_schema_location_accepts_local_kubeconform_template() -> None:
    location = "charts/demo/schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"

    assert _spec(schemaLocations=[location]).schema_locations == [location]


@pytest.mark.parametrize("kinds", [[""], ["Widget/v1"], ["two words"], ["Widget", "Widget"]])
def test_ignore_missing_schemas_requires_unique_kind_names(kinds: list[str]) -> None:
    with pytest.raises(ValidationError, match="ignoreMissingSchemas"):
        _spec(ignoreMissingSchemas=kinds)


@pytest.mark.parametrize(
    "legacy",
    [
        "release_name",
        "namespace_template",
        "helm_version",
        "helm_bin",
        "kubernetesVersion",
        "kubernetes_version",
        "schema_locations",
        "ignore_missing_schemas",
        "trigger_ignores",
        "triggers_strict",
        "skip",
        "version",
    ],
)
def test_rejects_removed_or_legacy_manifest_field_names(legacy: str) -> None:
    raw: dict[str, object] = {
        "releaseName": "demo",
        "environments": {"dev": {"namespace": "dev"}},
        legacy: True,
    }
    if legacy == "release_name":
        raw.pop("releaseName")
        raw[legacy] = "demo"

    with pytest.raises(ValidationError):
        ManifestValidationSpec.model_validate(raw)


def test_rejects_both_helm_bindings() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        _spec(helmVersion="4.1.3", helmBinary="/opt/helm")


def test_requires_release_name() -> None:
    with pytest.raises(ValidationError):
        ManifestValidationSpec.model_validate(
            {"environments": {"dev": {"namespace": "dev"}}}
        )


def test_requires_namespace_or_template() -> None:
    with pytest.raises(ValidationError, match="namespaceTemplate"):
        ManifestValidationSpec.model_validate(
            {"releaseName": "demo", "environments": {"dev": {}}}
        )


def test_namespace_template_substitution_and_override() -> None:
    spec = _spec(
        environments={
            "dev": {"values": ["values.yaml"]},
            "prod": {"namespace": "lab-prod-explicit"},
        }
    )

    assert resolve_namespace(spec, "dev") == "lab-dev"
    assert resolve_namespace(spec, "prod") == "lab-prod-explicit"


def test_resolve_namespace_rejects_unknown_environment() -> None:
    with pytest.raises(SpecError, match="unknown environment"):
        resolve_namespace(_spec(), "nope")


def test_trigger_string_must_be_a_known_alias() -> None:
    # The TriggerValue Literal rejects an unknown alias before _check_triggers.
    with pytest.raises(
        ValidationError,
        match="Input should be 'match-by-basename' or 'all-environments'",
    ):
        _spec(triggers={"values.yaml": "bogus"})


def test_trigger_accepts_all_environments_alias() -> None:
    spec = _spec(triggers={"templates/**": "all-environments"})

    assert spec.triggers["templates/**"] == ALL_ENVIRONMENTS


def test_trigger_environment_must_exist() -> None:
    with pytest.raises(ValidationError, match="unknown environment"):
        _spec(triggers={"values.yaml": ["staging"]})


@pytest.mark.parametrize("pattern", ["/tmp/**", "../README.md", "docs/../../README.md"])
def test_trigger_ignore_patterns_must_stay_inside_chart(pattern: str) -> None:
    with pytest.raises(ValidationError, match="trigger ignore pattern"):
        _spec(triggerIgnores=[pattern])


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "../../secrets.yaml", "envs/../../../etc/hosts"],
)
def test_environment_values_must_stay_inside_chart(bad: str) -> None:
    with pytest.raises(ValidationError, match="chart-relative"):
        _spec(environments={"dev": {"values": [bad]}})


def test_policy_paths_must_stay_inside_chart() -> None:
    with pytest.raises(ValidationError, match="chart-relative"):
        _spec(policies={"extra": ["../../../policies"]})


def test_unmatched_changes_defaults_to_warn() -> None:
    assert _spec().unmatched_changes == "warn"


def test_unmatched_changes_rejects_unknown_policy() -> None:
    with pytest.raises(ValidationError):
        _spec(unmatchedChanges="ignore")
