"""Manifest-validation ``resolve_namespace()`` matrix."""

from __future__ import annotations

import pytest

from chart_manager.api.lifecycle.v1alpha1 import ManifestValidationSpec
from chart_manager.plumbing.errors import SpecError
from chart_manager.services.manifest_validation.spec import resolve_namespace


def _spec(**kwargs) -> ManifestValidationSpec:
    base = {
        "releaseName": "x",
        "environments": {"dev": {"namespace": "lab-dev", "values": ["values.yaml"]}},
    }
    base.update(kwargs)
    return ManifestValidationSpec.model_validate(base)


def test_explicit_namespace_wins_over_template() -> None:
    s = _spec(
        namespaceTemplate="lab-${env}",
        environments={
            "dev": {"namespace": "explicit-dev", "values": ["values.yaml"]},
        },
    )
    assert resolve_namespace(s, "dev") == "explicit-dev"


def test_template_substitution_when_namespace_absent() -> None:
    s = _spec(
        namespaceTemplate="lab-${env}",
        environments={
            "dev": {"values": ["values.yaml"]},
            "prod": {"values": ["values.yaml"]},
        },
    )
    assert resolve_namespace(s, "dev") == "lab-dev"
    assert resolve_namespace(s, "prod") == "lab-prod"


def test_explicit_namespace_no_template_works() -> None:
    s = _spec()
    assert resolve_namespace(s, "dev") == "lab-dev"


def test_neither_set_is_a_validator_error() -> None:
    # The model validator catches "no template + no per-env namespace"
    # before resolve_namespace is ever called.
    with pytest.raises(ValueError, match="namespaceTemplate"):
        ManifestValidationSpec.model_validate(
            {
                "releaseName": "x",
                "environments": {"dev": {"values": ["values.yaml"]}},
            }
        )


def test_unknown_env_raises_specerror() -> None:
    s = _spec(namespaceTemplate="lab-${env}")
    with pytest.raises(SpecError):
        resolve_namespace(s, "nope")
