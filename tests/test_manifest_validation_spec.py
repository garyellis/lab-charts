"""Unit tests for the nested ``ManifestValidationSpec`` capability model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chart_manager.api.lifecycle.v1alpha1 import (
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
            "kubernetesVersion": "1.31.2",
            "schemaLocations": ["default"],
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
    assert spec.kubernetes_version == "1.31.2"
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
    "legacy",
    [
        "release_name",
        "namespace_template",
        "helm_version",
        "helm_bin",
        "kubernetes_version",
        "schema_locations",
        "trigger_ignores",
        "triggers_strict",
        "skip",
        "version",
    ],
)
def test_rejects_legacy_manifest_field_names(legacy: str) -> None:
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


def test_trigger_string_must_be_match_by_basename() -> None:
    with pytest.raises(ValidationError, match="match-by-basename"):
        _spec(triggers={"values.yaml": "bogus"})


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
