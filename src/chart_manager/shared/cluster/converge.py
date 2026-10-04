"""Install one Helm release into a session's cluster and wait until it is ready.

The wait covers the release's own Deployments, StatefulSets and DaemonSets (from
`helm get manifest`), the ones labelled with its instance (which catches workloads an
operator creates from the release's custom resources), and its CRDs becoming
`Established`. Anything a chart needs beyond that is its `helmTest` or hooks.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from chart_manager.plumbing.errors import (
    ChartManagerError,
    ExternalCommandError,
    MissingToolError,
    SpecError,
)
from chart_manager.plumbing.yaml_files import parse_yaml_documents
from chart_manager.shared.cluster.session import Session

WORKLOAD_KINDS = ("deployment", "statefulset", "daemonset")
#: The install and wait budget when a release names none.
DEFAULT_TIMEOUT = "10m"

Step = Literal["dependency update", "install", "wait"]


@dataclass(frozen=True)
class Release:
    """One Helm release to install: a chart directory, or a remote chart reference."""

    name: str
    chart: Path | str
    namespace: str
    values: tuple[Path, ...] = ()
    sets: Mapping[str, str] = field(default_factory=dict)
    timeout: str = DEFAULT_TIMEOUT
    version: str | None = None
    repo: str | None = None


class ReleaseFailed(ExternalCommandError):
    """A release failed to install or become ready; carries the namespace diagnostics."""

    def __init__(
        self, release: Release, step: Step, cause: ChartManagerError, diagnostics: str
    ) -> None:
        super().__init__(
            f"{release.name} -> {release.namespace}: {step} failed: {cause}",
            stderr=getattr(cause, "stderr", ""),
            returncode=getattr(cause, "returncode", None),
        )
        self.release = release
        self.step = step
        self.diagnostics = diagnostics


def converge(session: Session, release: Release) -> Literal["applied", "no-change"]:
    """Install `release` and wait for it; return whether Helm changed anything.

    Raises `ReleaseFailed` with diagnostics when a step fails. A missing tool or a
    configuration error is raised as it is: it is not this release's failure.
    """
    step: Step = "dependency update"
    try:
        if isinstance(release.chart, Path):
            session.helm.dependency_update_if_stale(release.chart)
        step = "install"
        result = session.helm.upgrade_install(
            release.name,
            release.chart,
            namespace=release.namespace,
            values=list(release.values),
            sets=dict(release.sets),
            timeout=release.timeout,
            wait=False,
            version=release.version,
            repo=release.repo,
        )
        step = "wait"
        wait(session, release)
    except (MissingToolError, SpecError):
        raise
    except ChartManagerError as exc:
        raise ReleaseFailed(
            release, step, exc, session.kubectl.diagnostics(release.namespace)
        ) from exc
    return result.status


def installed(session: Session) -> dict[tuple[str, str], str]:
    """Every Helm release in the cluster, in any state: (namespace, name) -> status."""
    return {
        (info.namespace, info.name): info.status
        for info in session.helm.list_releases(all_namespaces=True, any_status=True)
    }


def wait(session: Session, release: Release) -> None:
    """Wait for the release's workloads to roll out and its CRDs to be Established."""
    workloads: dict[tuple[str, str, str], None] = {}
    crds: list[str] = []
    for document in parse_yaml_documents(
        session.helm.manifest(release.name, namespace=release.namespace),
        source=f"helm get manifest {release.name}",
    ):
        if not isinstance(document, dict):
            continue
        kind = str(document.get("kind", "")).lower()
        metadata = document.get("metadata") or {}
        name = metadata.get("name")
        if not name:
            continue
        if kind in WORKLOAD_KINDS:
            workloads[(kind, metadata.get("namespace") or release.namespace, name)] = None
        elif kind == "customresourcedefinition":
            crds.append(name)
    selector = f"app.kubernetes.io/instance={release.name}"
    for kind in WORKLOAD_KINDS:
        for name in session.kubectl.workload_names(
            kind, namespace=release.namespace, selector=selector
        ):
            workloads[(kind, release.namespace, name)] = None
    for kind, namespace, name in sorted(workloads, key=lambda w: WORKLOAD_KINDS.index(w[0])):
        session.kubectl.rollout_status(kind, name, namespace=namespace, timeout=release.timeout)
    for crd in crds:
        session.kubectl.wait_established(crd, timeout=release.timeout)
