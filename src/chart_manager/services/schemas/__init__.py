"""Locked, content-addressed Kubernetes schema synchronization.

Public types live in their cohesive submodules. Keeping package initialization
side-effect free also lets source integrations depend on typed schema errors
without creating an integrations-to-sync circular import.
"""
