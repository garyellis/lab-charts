"""Resolve the namespace an authored validation environment renders into.

The accepted shape of ``spec.validation`` is owned by
``chart_manager.api.lifecycle.v1alpha1``; this is application interpretation
of a document that already parsed, so it raises ``SpecError`` rather than a
Pydantic error.

It is a leaf module rather than a function on
``manifest_validation.compiler`` because ``planner`` needs it too, and
``planner`` -- along with ``lifecycle.impact``, which imports ``planner`` --
is currently free of ``chart_manager.integrations``.  Importing the compiler
would drag the helm, kubeconform and kyverno adapters into both for the sake
of one pure string substitution.
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
