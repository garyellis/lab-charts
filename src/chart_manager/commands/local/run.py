"""`local up/reset/down/status`: converge a chart or LocalStack onto the persistent cluster.

Everything authored is loaded and checked before the cluster is touched. Bootstrap is
fail-fast; the target's releases are converged one by one, and a failed release is
recorded while the rest carry on.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from chart_manager.api.v1alpha1.local_cluster import LocalCluster
from chart_manager.api.v1alpha1.releases import (
    LifecycleRelease,
    OciChartRelease,
    RepoChartRelease,
)
from chart_manager.commands.local.access import access_hints, wait_apps_wildcard_ready
from chart_manager.commands.local.drift import warn_on_port_mapping_drift
from chart_manager.commands.local.models import (
    DevClusterActionResult,
    DevClusterEntryFailure,
    DevClusterEntryOutcome,
    DevClusterPlan,
    DevClusterPlanEntry,
    DevClusterResult,
    DevClusterStatus,
    RunSummary,
)
from chart_manager.commands.local.status import cluster_status
from chart_manager.commands.local.targets import ResolvedLocalTarget
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.shared.charts.chart import ResolvedChartTarget
from chart_manager.shared.charts.chart_tests import ChartTestCatalog
from chart_manager.shared.charts.install_plan import InstallPlanEntry
from chart_manager.shared.charts.lifecycle import require_chart_test_profile
from chart_manager.shared.cluster import bootstrap
from chart_manager.shared.cluster.bootstrap import ExternallySatisfiedLifecycle
from chart_manager.shared.cluster.converge import Release, ReleaseFailed, converge, installed
from chart_manager.shared.cluster.local_cluster import load_cluster
from chart_manager.shared.cluster.progress import (
    ProgressCallback,
    detail,
    emit,
    failure,
    info,
    step,
    warn,
)
from chart_manager.shared.cluster.releases import (
    helm_release,
    lifecycle_install_plan,
)
from chart_manager.shared.cluster.session import (
    DEFAULT_CLUSTER_NAME,
    Session,
    attach,
    find,
    kind_config_path,
    provision,
    stop,
)
from chart_manager.shared.settings import Settings
from chart_manager.shared.workspace import RepositoryWorkspace

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _LifecycleStep:
    """A lifecycle release resolved to its install plan."""

    catalog: ChartTestCatalog
    plan: tuple[InstallPlanEntry, ...]


type _Step = _LifecycleStep | OciChartRelease | RepoChartRelease


@dataclass(frozen=True)
class _Prepared:
    cluster: LocalCluster
    steps: tuple[_Step, ...]


def up(
    target: ResolvedLocalTarget,
    *,
    workspace: RepositoryWorkspace,
    runner: CommandRunner,
    settings: Settings,
    profile: str | None = None,
    skip_installed: bool = False,
    run_hooks: bool = True,
    progress: ProgressCallback | None = None,
) -> DevClusterResult:
    """Create or start the cluster, bootstrap it, then converge the chart or stack.

    `skip_installed` skips releases `helm list` shows as deployed or failed.
    """
    prepared = _prepare(target, profile, workspace, progress)
    session = provision(
        prepared.cluster,
        root=workspace.root,
        name=DEFAULT_CLUSTER_NAME,
        run_hooks=run_hooks,
        runner=runner,
        settings=settings,
        progress=progress,
    )
    return _converge(
        session, prepared, workspace.root, skip_installed=skip_installed, progress=progress
    )


def reset(
    target: ResolvedLocalTarget,
    *,
    workspace: RepositoryWorkspace,
    runner: CommandRunner,
    settings: Settings,
    profile: str | None = None,
    run_hooks: bool = True,
    progress: ProgressCallback | None = None,
) -> DevClusterResult:
    """Delete the cluster and converge the chart or stack onto a new one.

    Everything authored is resolved before the healthy cluster is deleted.
    """
    prepared = _prepare(target, profile, workspace, progress)
    session = provision(
        prepared.cluster,
        root=workspace.root,
        name=DEFAULT_CLUSTER_NAME,
        run_hooks=run_hooks,
        runner=runner,
        settings=settings,
        replace=True,
        progress=progress,
    )
    return _converge(session, prepared, workspace.root, skip_installed=False, progress=progress)


def down(
    *, runner: CommandRunner, settings: Settings, progress: ProgressCallback | None = None
) -> DevClusterActionResult:
    """Stop the cluster's nodes, keeping etcd, Helm releases, PVCs and the image cache."""
    emit(progress, step("Stopping dev cluster", DEFAULT_CLUSTER_NAME))
    stopped = stop(attach(DEFAULT_CLUSTER_NAME, runner=runner, settings=settings))
    _LOG.info("dev cluster stopped: cluster=%s changed=%s", DEFAULT_CLUSTER_NAME, stopped)
    return DevClusterActionResult(cluster_name=DEFAULT_CLUSTER_NAME, changed=stopped)


def status(
    *, workspace: RepositoryWorkspace, runner: CommandRunner, settings: Settings
) -> DevClusterStatus:
    """Whether the cluster exists, its releases, URLs and port-mapping drift; never raises."""
    return cluster_status(
        find(DEFAULT_CLUSTER_NAME, runner=runner, settings=settings),
        name=DEFAULT_CLUSTER_NAME,
        kind=attach(DEFAULT_CLUSTER_NAME, runner=runner, settings=settings).kind,
        root=workspace.root,
        config=_authored_kind_config(workspace),
    )


def plan(
    target: ResolvedLocalTarget,
    *,
    workspace: RepositoryWorkspace,
    profile: str | None,
    destroys: bool = False,
    run_hooks: bool = True,
    progress: ProgressCallback | None = None,
) -> DevClusterPlan:
    """What `up` (or `reset`, with `destroys`) would install; asks no cluster anything.

    Runs the same preflight as the real command, so a plan that cannot resolve fails
    the same way. Bootstrap entries are sorted rather than in authored order.
    """
    cluster = load_cluster(workspace)
    owned = bootstrap.preflight(cluster, root=workspace.root)
    steps = _preflight(
        _target_releases(target, profile, workspace.root),
        owned,
        workspace.root,
        progress,
    )
    entries = [
        DevClusterPlanEntry(i.chart, i.profile, i.namespace, "bootstrap")
        for i in sorted(owned, key=lambda i: (i.chart, i.profile, i.namespace))
    ]
    for target_step in steps:
        if isinstance(target_step, _LifecycleStep):
            for entry in target_step.plan:
                chart = target_step.catalog.get(entry.chart)
                namespace = require_chart_test_profile(chart.spec, entry.profile).namespace
                entries.append(DevClusterPlanEntry(entry.chart, entry.profile, namespace, "target"))
            continue
        _release, label = helm_release(target_step, workspace.root)
        entries.append(
            DevClusterPlanEntry(target_step.name, label, target_step.namespace, "target")
        )
    hooks = cluster.spec.cluster.hooks
    return DevClusterPlan(
        command="reset" if destroys else "up",
        cluster_name=DEFAULT_CLUSTER_NAME,
        target=target.name,
        target_kind=target.kind,
        destroys=destroys,
        entries=tuple(entries),
        provisioning_hooks_enabled=run_hooks,
        provisioning_hooks=(
            ()
            if hooks is None
            else tuple(
                (phase, tuple(command))
                for phase, command in (
                    ("preProvision", hooks.pre_provision),
                    ("postProvision", hooks.post_provision),
                )
                if command is not None
            )
        ),
    )


def plan_down() -> DevClusterPlan:
    """The plan for `down`: stop this cluster, install nothing."""
    return DevClusterPlan(command="down", cluster_name=DEFAULT_CLUSTER_NAME)


def _prepare(
    target: ResolvedLocalTarget,
    profile: str | None,
    workspace: RepositoryWorkspace,
    progress: ProgressCallback | None,
) -> _Prepared:
    cluster = load_cluster(workspace)
    owned = bootstrap.preflight(cluster, root=workspace.root)
    releases = _target_releases(target, profile, workspace.root)
    return _Prepared(cluster, _preflight(releases, owned, workspace.root, progress))


def _converge(
    session: Session,
    prepared: _Prepared,
    root: Path,
    *,
    skip_installed: bool,
    progress: ProgressCallback | None,
) -> DevClusterResult:
    started = time.monotonic()
    summary = RunSummary()
    installed_keys = _installed_keys(session, progress)
    try:
        outcomes = bootstrap.bootstrap(session, prepared.cluster, root=root, progress=progress)
    except ReleaseFailed as exc:
        if exc.diagnostics.strip():
            emit(progress, info(exc.diagnostics))
        raise
    for outcome in outcomes:
        bucket = summary.applied if outcome.status == "applied" else summary.no_change
        bucket.append(DevClusterEntryOutcome(outcome.name, outcome.profile, outcome.namespace))
        installed_keys.add((outcome.namespace, outcome.name))
    for target_step in prepared.steps:
        if isinstance(target_step, _LifecycleStep):
            releases = _lifecycle_releases(target_step, summary, progress)
        else:
            releases = [helm_release(target_step, root)]
        for release, label in releases:
            _converge_one(
                session, release, label, installed_keys, summary, skip_installed, progress
            )
    wait_apps_wildcard_ready(summary, kubectl=session.kubectl, progress=progress)
    warn_on_port_mapping_drift(
        session.name,
        kind=session.kind,
        root=root,
        progress=progress,
        config=kind_config_path(root, prepared.cluster),
    )
    _LOG.info(
        "local converge finished: cluster=%s applied=%d no_change=%d failed=%d elapsed=%.1fs",
        session.name,
        len(summary.applied),
        len(summary.no_change),
        len(summary.failed),
        time.monotonic() - started,
    )
    return summary.freeze(access_hints(summary, kubectl=session.kubectl))


def _installed_keys(session: Session, progress: ProgressCallback | None) -> set[tuple[str, str]]:
    """Releases `--skip-installed` skips: deployed or failed, as `helm list` shows them.

    A failed listing falls back to "nothing installed" rather than aborting.
    """
    try:
        releases = installed(session)
    except ChartManagerError as exc:
        _LOG.warning("helm release listing failed; treating every release as uninstalled: %s", exc)
        emit(
            progress,
            warn(f"could not list helm releases ({exc}); proceeding as if no releases exist"),
        )
        return set()
    return {key for key, state in releases.items() if state in {"deployed", "failed"}}


def _lifecycle_releases(
    target_step: _LifecycleStep, summary: RunSummary, progress: ProgressCallback | None
) -> list[tuple[Release, str]]:
    """Each plan entry as a release; an entry that does not resolve is a failed row."""
    releases = []
    for entry in target_step.plan:
        try:
            chart = target_step.catalog.get(entry.chart)
            profile = require_chart_test_profile(chart.spec, entry.profile)
            values = target_step.catalog.value_paths(chart, entry.profile)
        except ChartManagerError as exc:
            _LOG.error(
                "chart resolution failed: chart=%s profile=%s: %s", entry.chart, entry.profile, exc
            )
            emit(progress, failure("chart resolution failed:", f"{entry.chart}: {exc}"))
            summary.failed.append(DevClusterEntryFailure(entry.chart, entry.profile, "?", str(exc)))
            continue
        release = Release(
            name=entry.chart,
            chart=chart.path,
            namespace=profile.namespace,
            values=tuple(values),
            timeout=profile.timeout,
        )
        releases.append((release, entry.profile))
    return releases


def _converge_one(
    session: Session,
    release: Release,
    label: str,
    installed_keys: set[tuple[str, str]],
    summary: RunSummary,
    skip_installed: bool,
    progress: ProgressCallback | None,
) -> None:
    key = (release.namespace, release.name)
    if skip_installed and key in installed_keys:
        emit(progress, detail("skip", f"{release.name} (already installed in {release.namespace})"))
        summary.no_change.append(DevClusterEntryOutcome(release.name, label, release.namespace))
        return
    emit(progress, step("Applying", f"{release.name}:{label} -> {release.namespace}"))
    try:
        state = converge(session, release)
    except ChartManagerError as exc:
        if isinstance(exc, ReleaseFailed) and exc.diagnostics.strip():
            emit(progress, info(exc.diagnostics))
        _LOG.error(
            "release failed; converge continues: release=%s profile=%s namespace=%s: %s",
            release.name,
            label,
            release.namespace,
            exc,
        )
        emit(progress, failure("apply failed:", f"{release.name}:{label} -> {exc}"))
        summary.failed.append(
            DevClusterEntryFailure(release.name, label, release.namespace, str(exc))
        )
        return
    bucket = summary.applied if state == "applied" else summary.no_change
    bucket.append(DevClusterEntryOutcome(release.name, label, release.namespace))
    installed_keys.add(key)


def _target_releases(
    target: ResolvedLocalTarget, profile: str | None, root: Path
) -> tuple[LifecycleRelease | OciChartRelease | RepoChartRelease, ...]:
    if isinstance(target, ResolvedChartTarget):
        return (
            LifecycleRelease(
                type="lifecycle",
                chart=target.path.relative_to(root.resolve()),
                profile=profile or "minimal",
            ),
        )
    if profile is not None:
        raise ChartManagerError(
            "--profile is only valid for a chart target; LocalStack releases declare their profiles"
        )
    return tuple(target.stack.spec.releases)


def _preflight(
    releases: tuple[LifecycleRelease | OciChartRelease | RepoChartRelease, ...],
    owned: frozenset[ExternallySatisfiedLifecycle],
    root: Path,
    progress: ProgressCallback | None,
) -> tuple[_Step, ...]:
    """Resolve every release before anything is installed, in authored order.

    A lifecycle release becomes its install plan, minus what bootstrap owns and what an
    earlier release already installs; the same chart under two identities is an error.
    """
    seen: dict[Path, tuple[str, str]] = {}
    steps: list[_Step] = []
    for release in releases:
        if isinstance(release, (OciChartRelease, RepoChartRelease)):
            steps.append(release)
            continue
        catalog, install_plan = lifecycle_install_plan(root, release, source="local release")
        kept: list[InstallPlanEntry] = []
        for entry in install_plan:
            chart = catalog.get(entry.chart)
            chart_path = chart.path.resolve()
            entry_profile = require_chart_test_profile(chart.spec, entry.profile)
            namespace = entry_profile.namespace
            if (
                ExternallySatisfiedLifecycle(chart_path, entry.chart, entry.profile, namespace)
                in owned
            ):
                continue
            identity = (entry.profile, namespace)
            previous = seen.get(chart_path)
            if previous == identity:
                continue
            if previous is not None:
                raise ChartManagerError(
                    f"conflicting local lifecycle identities for {entry.chart}: "
                    f"first {previous[0]} in {previous[1]}, then {entry.profile} in {namespace}"
                )
            seen[chart_path] = identity
            if entry_profile.hooks is not None:
                message = (
                    f"local up does not run chart-test hooks declared by "
                    f"{entry.chart}:{entry.profile}"
                )
                _LOG.warning("%s", message)
                emit(progress, warn(message))
            kept.append(entry)
        steps.append(_LifecycleStep(catalog, tuple(kept)))
    return tuple(steps)


def _authored_kind_config(workspace: RepositoryWorkspace) -> Path | None:
    """The LocalCluster's kind config, or None when it cannot be read.

    `status` answers even without a valid LocalCluster; the drift check then has no
    baseline, which is logged rather than raised.
    """
    try:
        return kind_config_path(workspace.root, load_cluster(workspace))
    except (ChartManagerError, OSError) as exc:
        _LOG.warning(
            "LocalCluster unreadable; port-mapping drift has no baseline: %s: %s",
            type(exc).__name__,
            exc,
        )
        return None
