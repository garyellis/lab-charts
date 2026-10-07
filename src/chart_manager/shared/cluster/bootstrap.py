"""The LocalCluster's ordered bootstrap releases, installed before any chart under test.

Each release goes through `converge`; its authored readiness gates (nodes Ready, then a
namespace's workloads) run after it, because a network chart is what makes nodes Ready.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from chart_manager.api.v1alpha1.local_cluster import LocalCluster
from chart_manager.api.v1alpha1.releases import (
    BootstrapLifecycleRelease,
    BootstrapRelease,
)
from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.plumbing.progress import ProgressCallback, emit, step
from chart_manager.shared.charts.dependency_update import ensure_dependencies
from chart_manager.shared.charts.lifecycle import require_chart_test_profile
from chart_manager.shared.cluster.converge import DEFAULT_TIMEOUT, Release, converge
from chart_manager.shared.cluster.releases import (
    chart_name,
    helm_release,
    lifecycle_install_plan,
)
from chart_manager.shared.cluster.session import Session


@dataclass(frozen=True)
class ExternallySatisfiedLifecycle:
    """Exact managed lifecycle identity already converged by an environment."""

    chart_path: Path
    chart: str
    profile: str
    namespace: str


@dataclass(frozen=True)
class BootstrapOutcome:
    """One converged bootstrap release."""

    name: str
    profile: str
    namespace: str
    status: str


def bootstrap(
    session: Session,
    cluster: LocalCluster,
    *,
    root: Path,
    progress: ProgressCallback | None = None,
) -> tuple[BootstrapOutcome, ...]:
    """Converge every bootstrap release in order; stop at the first that fails."""
    root = root.resolve()
    outcomes: list[BootstrapOutcome] = []
    for authored in cluster.spec.bootstrap.releases:
        sets = _runtime_values(session, authored)
        for release, profile in _releases(root, authored, sets):
            emit(progress, step("Bootstrapping", f"{release.name} -> {release.namespace}"))
            status = converge(session, release)
            outcomes.append(BootstrapOutcome(release.name, profile, release.namespace, status))
        _wait_ready(session, authored, progress)
    return tuple(outcomes)


def verify(cluster: LocalCluster, *, root: Path, releases: Mapping[tuple[str, str], str]) -> None:
    """Require every bootstrap release among `releases` (any state), without installing."""
    for authored in cluster.spec.bootstrap.releases:
        for release, _profile in _releases(root.resolve(), authored, {}):
            if (release.namespace, release.name) not in releases:
                raise ChartManagerError(
                    f"bootstrap release {release.name!r} is not installed in namespace "
                    f"{release.namespace!r}; rerun without --skip-requires to converge it"
                )


def preflight(
    cluster: LocalCluster, *, root: Path, helm: Helm | None = None
) -> frozenset[ExternallySatisfiedLifecycle]:
    """Resolve every lifecycle bootstrap plan before the cluster is touched.

    Returns the chart/profile/namespace identities bootstrap owns, so the chart under
    test does not install them again. With `helm`, every bootstrap chart is linted once
    all of them have resolved.
    """
    root = root.resolve()
    identities: set[ExternallySatisfiedLifecycle] = set()
    lint_targets: list[tuple[Path, list[Path]]] = []
    for release in cluster.spec.bootstrap.releases:
        if not isinstance(release, BootstrapLifecycleRelease):
            continue
        catalog, plan = lifecycle_install_plan(root, release, source="bootstrap chart")
        for entry in plan:
            chart = catalog.get(entry.chart)
            profile = require_chart_test_profile(chart.spec, entry.profile)
            # Bootstrap bypasses the compiled plan: refuse hooks, don't drop them.
            if profile.hooks is not None:
                raise SpecError(
                    f"bootstrap chart {entry.chart}:{entry.profile} declares "
                    "chart-test hooks, which bootstrap does not run"
                )
            identities.add(
                ExternallySatisfiedLifecycle(
                    chart_path=chart.path.resolve(),
                    chart=entry.chart,
                    profile=entry.profile,
                    namespace=profile.namespace,
                )
            )
            lint_targets.append((chart.path, catalog.value_paths(chart, entry.profile)))
    if helm is not None:
        for chart_path, values in lint_targets:
            ensure_dependencies(helm, chart_path)
            helm.lint(chart_path, values)
    return frozenset(identities)


def _releases(
    root: Path, authored: BootstrapRelease, sets: dict[str, str]
) -> list[tuple[Release, str]]:
    """The Helm releases one authored bootstrap release installs, with their row label."""
    if isinstance(authored, BootstrapLifecycleRelease):
        catalog, plan = lifecycle_install_plan(root, authored, source="bootstrap chart")
        root_chart = chart_name(root, authored.chart)
        releases = []
        for entry in plan:
            chart = catalog.get(entry.chart)
            profile = require_chart_test_profile(chart.spec, entry.profile)
            is_root = entry.chart == root_chart and entry.profile == authored.profile
            releases.append(
                (
                    Release(
                        name=entry.chart,
                        chart=chart.path,
                        namespace=profile.namespace,
                        values=tuple(catalog.value_paths(chart, entry.profile)),
                        sets=sets if is_root else {},
                        timeout=profile.timeout,
                    ),
                    entry.profile,
                )
            )
        return releases
    return [helm_release(authored, root, sets=sets)]


def _runtime_values(session: Session, release: BootstrapRelease) -> dict[str, str]:
    if not release.runtime_values:
        return {}
    facts = {
        "${kind.controlPlanePort}": "6443",
        "${kind.clusterName}": session.name,
        "${kind.context}": session.context,
    }
    if "${kind.controlPlaneHost}" in release.runtime_values.values():
        facts["${kind.controlPlaneHost}"] = session.kind.control_plane_ip(session.name)
    return {key: facts[value] for key, value in release.runtime_values.items()}


def _wait_ready(
    session: Session, release: BootstrapRelease, progress: ProgressCallback | None
) -> None:
    readiness = release.readiness
    if readiness is None:
        return
    gate = readiness.workloads_ready
    timeout = gate.timeout if gate is not None else getattr(release, "timeout", DEFAULT_TIMEOUT)
    if readiness.nodes_ready:
        emit(progress, step("Waiting for cluster nodes"))
        session.kubectl.wait_nodes_ready(timeout=timeout)
    if gate is not None:
        emit(progress, step("Waiting for bootstrap workloads", gate.namespace))
        session.kubectl.wait_workloads_ready(gate.namespace, timeout=gate.timeout)
