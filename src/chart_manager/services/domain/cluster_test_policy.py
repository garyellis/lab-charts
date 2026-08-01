"""Lookup policy over the authored cluster-test section.

``ClusterTestSpec`` used to carry a ``profile()`` method, which put an
application exception (``SpecError``) and a "list the alternatives" diagnostic
on a model whose only job is to describe accepted YAML.  The lookup lives here
instead so ``chart_manager.api`` stays pure representation.

This sits in ``services/domain`` rather than beside ``ClusterTestCatalog``
because ``services/domain/install_plan`` is one of its callers: a domain
algorithm importing a top-level service module would invert the dependency
direction the layering contract relies on.
"""

from __future__ import annotations

from chart_manager.api.lifecycle.v1alpha1 import ClusterTestProfile, ClusterTestSpec
from chart_manager.plumbing.errors import SpecError


def require_cluster_test_profile(spec: ClusterTestSpec, name: str) -> ClusterTestProfile:
    """Look up a profile by name; SpecError lists available names."""
    try:
        return spec.profiles[name]
    except KeyError as exc:
        profiles = ", ".join(sorted(spec.profiles))
        raise SpecError(f"unknown profile '{name}'. available profiles: {profiles}") from exc
