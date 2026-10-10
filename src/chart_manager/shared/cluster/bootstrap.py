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
from chart_manager.plumbing.duration import parse_duration
from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.plumbing.progress import Progress, step
from chart_manager.shared.charts.dependency_update import ensure_dependencies
from chart_manager.shared.charts.install_plan import InstallPlanEntry, install_plan
from chart_manager.shared.cluster.converge import DEFAULT_TIMEOUT, Release, converge
from chart_manager.shared.cluster.releases import helm_release, release
from chart_manager.shared.cluster.session import Session


@dataclass(frozen=True)
class ExternallySatisfiedLifecycle:
    """Exact managed lifecycle identity already converged by an environment."""

    chart_path: Path
    chart: str
    profile: str
    namespace: str

    @classmethod
    def of(cls, entry: InstallPlanEntry) -> ExternallySatisfiedLifecycle:
        """The identity an install-plan entry installs."""
        return cls(entry.chart.path.resolve(), entry.chart.name, entry.profile, entry.namespace)


@dataclass(frozen=True)
class BootstrapStep:
    """One authored bootstrap release; a lifecycle release carries its resolved install plan."""

    authored: BootstrapRelease
    entries: tuple[InstallPlanEntry, ...]


@dataclass(frozen=True)
class BootstrapOutcome:
    """One converged bootstrap release."""

    name: str
    profile: str
    namespace: str
    status: str


def bootstrap(
    session: Session,
    steps: tuple[BootstrapStep, ...],
    *,
    root: Path,
    progress: Progress,
) -> tuple[BootstrapOutcome, ...]:
    """Converge every bootstrap release in order; stop at the first that fails."""
    root = root.resolve()
    outcomes: list[BootstrapOutcome] = []
    for bootstrap_step in steps:
        sets = _runtime_values(session, bootstrap_step.authored)
        for planned, label in _releases(bootstrap_step, root, sets):
            progress(step("Bootstrapping", f"{planned.name} -> {planned.namespace}"))
            status = converge(session, planned)
            outcomes.append(BootstrapOutcome(planned.name, label, planned.namespace, status))
        _wait_ready(session, bootstrap_step.authored, progress)
    return tuple(outcomes)


def verify(
    steps: tuple[BootstrapStep, ...], *, root: Path, releases: Mapping[tuple[str, str], str]
) -> None:
    """Require every bootstrap release among `releases` (any state), without installing."""
    for bootstrap_step in steps:
        for required, _label in _releases(bootstrap_step, root.resolve(), {}):
            if (required.namespace, required.name) not in releases:
                raise ChartManagerError(
                    f"bootstrap release {required.name!r} is not installed in namespace "
                    f"{required.namespace!r}; rerun without --skip-requires to converge it"
                )


def preflight(
    cluster: LocalCluster, *, root: Path, helm: Helm | None = None
) -> tuple[BootstrapStep, ...]:
    """Resolve every bootstrap release before the cluster is touched.

    A lifecycle release resolves to its install plan; one whose plan declares chart-test
    hooks is refused, since bootstrap installs outside the compiled plan. With `helm`,
    every bootstrap chart is linted once all of them have resolved.
    """
    root = root.resolve()
    steps = []
    for authored in cluster.spec.bootstrap.releases:
        entries: tuple[InstallPlanEntry, ...] = ()
        if isinstance(authored, BootstrapLifecycleRelease):
            entries = tuple(
                install_plan(root / authored.chart.parent, authored.chart.name, authored.profile)
            )
        for entry in entries:
            if entry.spec.hooks is not None:
                raise SpecError(
                    f"bootstrap chart {entry.chart.name}:{entry.profile} declares "
                    "chart-test hooks, which bootstrap does not run"
                )
        steps.append(BootstrapStep(authored, entries))
    if helm is not None:
        for bootstrap_step in steps:
            for entry in bootstrap_step.entries:
                ensure_dependencies(helm, entry.chart.path)
                helm.lint(entry.chart.path, list(entry.values))
    return tuple(steps)


def owned(steps: tuple[BootstrapStep, ...]) -> frozenset[ExternallySatisfiedLifecycle]:
    """The lifecycle identities bootstrap installs, so a target does not install them again."""
    return frozenset(
        ExternallySatisfiedLifecycle.of(entry)
        for bootstrap_step in steps
        for entry in bootstrap_step.entries
    )


def _releases(
    bootstrap_step: BootstrapStep, root: Path, sets: dict[str, str]
) -> list[tuple[Release, str]]:
    """The Helm releases one bootstrap step installs, with their row label.

    Runtime values go only to the authored release, which is its install plan's last entry.
    """
    authored = bootstrap_step.authored
    if isinstance(authored, BootstrapLifecycleRelease):
        last = bootstrap_step.entries[-1]
        return [
            (release(entry, sets=sets if entry is last else {}), entry.profile)
            for entry in bootstrap_step.entries
        ]
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


def _wait_ready(session: Session, release: BootstrapRelease, progress: Progress) -> None:
    readiness = release.readiness
    if readiness is None:
        return
    gate = readiness.workloads_ready
    timeout = parse_duration(
        gate.timeout if gate is not None else getattr(release, "timeout", DEFAULT_TIMEOUT)
    )
    if readiness.nodes_ready:
        progress(step("Waiting for cluster nodes"))
        session.kubectl.wait_nodes_ready(timeout=timeout)
    if gate is not None:
        progress(step("Waiting for bootstrap workloads", gate.namespace))
        session.kubectl.wait_workloads_ready(gate.namespace, timeout=timeout)
