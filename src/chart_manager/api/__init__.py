"""Authored, versioned configuration contracts for chart-manager.

Every YAML document a user writes is described by exactly one module under
``chart_manager.api.<group>.v1alpha1``.  Reviewing a group's version module
shows the complete accepted shape -- field names, aliases, defaults, enums,
and the validation that can be decided from a single document -- without
reading loaders, filesystem resolution, planning, or CLI code.

This package may import the standard library, Pydantic, and side-effect-free
lexical helpers from ``chart_manager.plumbing``.  It must not import
``services``, ``integrations``, ``cli``, the composition root, repository
settings, Rich, or Typer, and its validators raise ``ValueError`` (or Pydantic
validation errors) rather than ``SpecError``: translating a decode failure
into a user-facing diagnostic is the loader's job.

Consumers import the explicit version::

    from chart_manager.api.lifecycle.v1alpha1 import ChartLifecycle

There are deliberately no versionless re-exports, so a future ``v1beta1``
cannot silently change what an existing consumer parses.
"""
