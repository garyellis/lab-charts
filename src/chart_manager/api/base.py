"""Shared Pydantic configuration for authored API models.

Intentionally minimal.  Only configuration that is *already* identical across
the groups that use it lives here; nothing that would harmonize two contracts
which differ today.  In particular the lifecycle and local groups spell
``metadata.name`` differently (``min_length=1`` plus a no-padding check versus
a DNS-label rule) and duration validation differently, so there is no shared
metadata model and no shared lexical validator here.

Two bases rather than one, because the accepted YAML is not uniformly strict:

``ApiModel``
    Rejects unknown authored keys, but lets Pydantic coerce scalars.  This is
    what the capability sections under ``spec`` do today -- ``enabled: "yes"``
    parses as ``True`` in ``spec.validation`` and ``spec.clusterTest``.

``StrictApiModel``
    Additionally refuses coercion, so ``enabled: "true"`` is a type error.
    This is what the ``ChartLifecycle`` envelope does today.

Collapsing the two would tighten seven models and reject YAML that parses
now, which is exactly the "premature unification" the refactor plan warns
about.  Neither base sets ``populate_by_name``: aliases are the only accepted
authored spelling, and adding it would create a second input surface.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    """Authored model that rejects unknown keys and coerces known ones."""

    model_config = ConfigDict(extra="forbid")


class StrictApiModel(ApiModel):
    """Authored model that also rejects values of the wrong type."""

    model_config = ConfigDict(strict=True)
