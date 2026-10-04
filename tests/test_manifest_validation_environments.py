"""The namespace a selected row renders into: explicit, else `namespaceTemplate`."""

from __future__ import annotations

import pytest

from chart_manager.api.v1alpha1.chart_lifecycle import ManifestValidationSpec
from chart_manager.commands.validate.select import selected_row


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
    assert selected_row("x", s, "dev").namespace == "explicit-dev"


def test_template_substitution_when_namespace_absent() -> None:
    s = _spec(
        namespaceTemplate="lab-${env}",
        environments={
            "dev": {"values": ["values.yaml"]},
            "prod": {"values": ["values.yaml"]},
        },
    )
    assert selected_row("x", s, "dev").namespace == "lab-dev"
    assert selected_row("x", s, "prod").namespace == "lab-prod"


def test_explicit_namespace_no_template_works() -> None:
    s = _spec()
    assert selected_row("x", s, "dev").namespace == "lab-dev"


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
