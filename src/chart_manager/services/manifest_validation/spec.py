"""Interpretation of an already-valid authored manifest-validation section.

The accepted shape of ``spec.validation`` is owned by
``chart_manager.api.lifecycle.v1alpha1``.  What remains here is application
interpretation of a document that already parsed: resolving the namespace an
environment renders into.
"""

from __future__ import annotations

from string import Template

from chart_manager.api.lifecycle.v1alpha1 import ManifestValidationSpec
from chart_manager.plumbing.errors import SpecError


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
