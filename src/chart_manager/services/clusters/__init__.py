"""Kubernetes cluster lifecycle services.

The package groups cluster-oriented capabilities without forcing them through
a shared executor:

* ``EphemeralTestClusterService`` is fail-fast and runs Helm tests.
* ``DevelopmentClusterService`` persistently converges a developer stack.
* ``bootstrap`` executes the generic LocalCluster bootstrap for both workflows.
* ``_shared`` holds the authored-configuration resolution all three do
  identically; ``environment`` owns the provider boundary and the shape both
  services are handed their cluster-bound clients in.
"""

from chart_manager.services.clusters.ephemeral import (
    EphemeralTestClusterService,
    EphemeralTestRequest,
    EphemeralTestResult,
)

__all__ = [
    "EphemeralTestClusterService",
    "EphemeralTestRequest",
    "EphemeralTestResult",
]
