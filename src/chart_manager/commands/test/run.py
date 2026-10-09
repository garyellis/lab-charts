"""Run `chart test`: provision a kind cluster, bootstrap it, then run one chart's plan.

A failed action is a result: the rest are skipped and the outcome carries the failing
namespace's diagnostics. A missing tool or a configuration error is raised.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace

from chart_manager.api.v1alpha1.local_cluster import LocalCluster
from chart_manager.commands.test.hooks import ChartTestHookRunner
from chart_manager.commands.test.models import (
    ActionKind,
    ActionOutcome,
    ChartTestOutcome,
    ChartTestRequest,
    LifecycleAction,
    LifecyclePlan,
    TeardownOutcome,
    TeardownRequest,
)
from chart_manager.commands.test.plan import (
    CompiledPlan,
    Requires,
    SkippedRequirement,
    compile_plan,
)
from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.duration import parse_duration
from chart_manager.plumbing.errors import ChartManagerError, MissingToolError, SpecError
from chart_manager.plumbing.progress import (
    Progress,
    detail,
    failure,
    info,
    step,
    warn,
)
from chart_manager.settings import Settings
from chart_manager.shared.charts.dependency_update import ensure_dependencies
from chart_manager.shared.cluster import bootstrap
from chart_manager.shared.cluster.bootstrap import BootstrapStep
from chart_manager.shared.cluster.converge import ReleaseFailed, converge, installed, wait
from chart_manager.shared.cluster.local_cluster import load_cluster
from chart_manager.shared.cluster.releases import release
from chart_manager.shared.cluster.session import Session, attach, find, provision
from chart_manager.shared.cluster.session import teardown as delete_cluster
from chart_manager.shared.workspace import RepositoryWorkspace

_LOG = logging.getLogger(__name__)

_LABELS = {
    ActionKind.NAMESPACE_ENSURE: "Ensuring namespace",
    ActionKind.HELM_LINT: "Linting",
    ActionKind.INSTALL: "Installing",
    ActionKind.WORKLOAD_READY: "Waiting for workloads",
    ActionKind.HELM_TEST: "Running Helm tests",
    ActionKind.HOOK_PRE_INSTALL: "Running pre-install hook",
    ActionKind.HOOK_POST_INSTALL: "Running post-install hook",
    ActionKind.HOOK_CLEANUP: "Running cleanup hook",
}


def plan(request: ChartTestRequest, *, workspace: RepositoryWorkspace) -> LifecyclePlan:
    """What `run` would do, without touching a cluster or linting (`--dry-run`).

    With `--skip-requires` the plan describes reusing an existing cluster, and warns
    about the fallback when none exists, which only a live cluster can decide.
    """
    requires: Requires = "skip" if request.skip_requires else "install"
    compiled, _ = _compile(
        request, workspace, load_cluster(workspace), lint_helm=None, requires=requires
    )
    if not request.skip_requires:
        return compiled.plan
    if request.ensure_cluster:
        behavior = (
            "--skip-requires bootstrap behavior: reuse verifies bootstrap and "
            "required releases without upgrading them; a missing cluster installs "
            "bootstrap and required charts but Helm-tests only selected targets"
        )
    else:
        behavior = (
            "--skip-requires bootstrap behavior: --no-ensure-cluster creates "
            "nothing and verifies existing bootstrap and required releases without "
            "upgrading them"
        )
    return replace(compiled.plan, warnings=(behavior, *compiled.plan.warnings))


def run(
    request: ChartTestRequest,
    *,
    workspace: RepositoryWorkspace,
    runner: CommandRunner,
    settings: Settings,
    progress: Progress,
) -> ChartTestOutcome:
    """Provision, bootstrap, install the chart's requirements and the chart, then test it.

    `--skip-requires` reuses an existing cluster: bootstrap and the requirements are
    checked as installed, not upgraded. On a cluster that does not exist yet they are
    installed, but only the selected charts are helm-tested.
    """
    started = time.monotonic()
    root = workspace.root
    cluster = load_cluster(workspace)
    name = request.cluster_name
    fresh_cluster = (
        request.skip_requires
        and request.ensure_cluster
        and find(name, runner=runner, settings=settings) is None
    )
    verify_only = request.skip_requires and not fresh_cluster
    lint_helm = (
        attach(name, runner=runner, settings=settings).helm
        if request.lint and not verify_only
        else None
    )
    requires: Requires = (
        "install-untested" if fresh_cluster else "skip" if request.skip_requires else "install"
    )
    compiled, bootstrap_steps = _compile(
        request, workspace, cluster, lint_helm=lint_helm, requires=requires
    )
    test_plan = compiled.plan
    if fresh_cluster:
        message = (
            f"cluster {name} does not exist; --skip-requires must install bootstrap and "
            "required charts before testing the selected target; required charts will "
            "not be Helm-tested"
        )
        _LOG.warning("%s", message)
        progress(warn(message))
    _LOG.info(
        "chart test started: chart=%s profile=%s cluster=%s actions=%d skip_requires=%s",
        request.chart,
        request.profile,
        name,
        len(test_plan.actions),
        request.skip_requires,
    )

    if request.ensure_cluster:
        session = provision(
            cluster,
            root=root,
            name=name,
            run_hooks=request.run_provision_hooks,
            runner=runner,
            settings=settings,
            progress=progress,
        )
    else:
        session = attach(name, runner=runner, settings=settings)

    outcome = ChartTestOutcome(chart=request.chart, profile=request.profile, cluster_name=name)
    if verify_only:
        bootstrap.verify(bootstrap_steps, root=root, releases=installed(session))
    else:
        try:
            bootstrap.bootstrap(session, bootstrap_steps, root=root, progress=progress)
        except ReleaseFailed as exc:
            if exc.diagnostics.strip():
                progress(info(exc.diagnostics))
            raise
    _require_skipped(request, compiled.skipped, session)

    hooks = ChartTestHookRunner(
        root, runner=runner, kube_context=session.context, cluster_name=name
    )
    actions, diagnostics = _execute(session, test_plan, hooks, progress)
    outcome = replace(outcome, actions=actions, diagnostics=diagnostics)
    _LOG.info(
        "chart test finished: chart=%s cluster=%s ok=%s elapsed=%.1fs",
        request.chart,
        name,
        outcome.ok,
        time.monotonic() - started,
    )
    return outcome


def teardown_plan(request: TeardownRequest, *, workspace: RepositoryWorkspace) -> LifecyclePlan:
    """The cleanup hooks `teardown` would run, in order; touches nothing."""
    compiled, _ = _compile(
        ChartTestRequest(
            chart=request.chart,
            profile=request.profile,
            namespace=request.namespace,
            cluster_name=request.cluster_name,
            include_dependent_tests=request.include_dependent_tests,
        ),
        workspace,
        load_cluster(workspace),
        lint_helm=None,
        requires="install",
    )
    cleanups = tuple(a for a in compiled.plan.actions if a.kind is ActionKind.HOOK_CLEANUP)
    return replace(compiled.plan, actions=cleanups)


def teardown(
    request: TeardownRequest,
    *,
    workspace: RepositoryWorkspace,
    runner: CommandRunner,
    settings: Settings,
    progress: Progress,
) -> TeardownOutcome:
    """Run every cleanup hook, continuing past failures, then delete the cluster unless kept."""
    cleanups = teardown_plan(request, workspace=workspace)
    name = request.cluster_name
    session = find(name, runner=runner, settings=settings)
    if session is None:
        message = f"cluster {name} does not exist; running cleanup hooks without a kube context"
        _LOG.warning("%s", message)
        progress(warn(message))
    hooks = ChartTestHookRunner(
        workspace.root,
        runner=runner,
        kube_context=session.context if session is not None else "",
        cluster_name=name,
    )
    outcomes = []
    for action in cleanups.actions:
        subject = _subject(action)
        progress(step(_LABELS[action.kind], subject))
        try:
            hooks.run(action)
        except ChartManagerError as exc:
            progress(failure("Failed", f"{subject}: {exc}"))
            outcomes.append(ActionOutcome(action.action_id, action.kind.value, "FAIL", str(exc)))
        else:
            progress(detail("Completed", subject))
            outcomes.append(ActionOutcome(action.action_id, action.kind.value, "PASS"))
    deleted, delete_error = False, None
    if session is not None and not request.keep_cluster:
        progress(step("Deleting test cluster", name))
        try:
            deleted = delete_cluster(session)
        except ChartManagerError as exc:
            delete_error = str(exc)
            _LOG.error("deleting cluster %s failed: %s", name, exc)
    return TeardownOutcome(
        cluster_name=name,
        cleanups=tuple(outcomes),
        cluster_deleted=deleted,
        delete_error=delete_error,
    )


def _compile(
    request: ChartTestRequest,
    workspace: RepositoryWorkspace,
    cluster: LocalCluster,
    *,
    lint_helm: Helm | None,
    requires: Requires,
) -> tuple[CompiledPlan, tuple[BootstrapStep, ...]]:
    """Resolve bootstrap (linting it with `lint_helm`), then compile the plan without it."""
    root = workspace.root
    steps = bootstrap.preflight(cluster, root=root, helm=lint_helm)
    compiled = compile_plan(
        request,
        root=root,
        charts_dir=workspace.charts_root,
        bootstrap_owned=bootstrap.owned(steps),
        requires=requires,
    )
    return compiled, steps


def _require_skipped(
    request: ChartTestRequest, skipped: tuple[SkippedRequirement, ...], session: Session
) -> None:
    """With `--skip-requires`, every requirement left out of the plan must be installed."""
    if not skipped:
        return
    releases = installed(session)
    for required in skipped:
        if (required.namespace, required.release) not in releases:
            raise ChartManagerError(
                f"{request.chart} requires {required.chart}:{required.profile}, "
                f"not installed in {required.namespace}; run once without --skip-requires"
            )


def _execute(
    session: Session,
    test_plan: LifecyclePlan,
    hooks: ChartTestHookRunner,
    progress: Progress,
) -> tuple[tuple[ActionOutcome, ...], str]:
    """Run each action once, skipping the rest after a failure; cleanups wait for teardown."""
    outcomes: list[ActionOutcome] = []
    diagnostics = ""
    failed = False
    for action in test_plan.actions:
        subject = _subject(action)
        if action.kind is ActionKind.HOOK_CLEANUP or failed:
            reason = "cleanup hooks run at teardown" if not failed else "an earlier action failed"
            progress(detail("Skipped", subject))
            outcomes.append(ActionOutcome(action.action_id, action.kind.value, "SKIP", reason))
            continue
        progress(step(_LABELS[action.kind], subject))
        try:
            _perform(session, action, hooks)
        except (MissingToolError, SpecError):
            raise
        except ChartManagerError as exc:
            failed = True
            diagnostics = _diagnostics(session, action, exc)
            _LOG.error(
                "cluster action failed: chart=%s action=%s namespace=%s: %s",
                action.entry.chart.name,
                action.action_id,
                action.entry.namespace,
                exc,
            )
            progress(failure("Failed", f"{subject}: {exc}"))
            if diagnostics.strip():
                progress(info(diagnostics))
            outcomes.append(ActionOutcome(action.action_id, action.kind.value, "FAIL", str(exc)))
            continue
        progress(detail("Completed", subject))
        outcomes.append(ActionOutcome(action.action_id, action.kind.value, "PASS"))
    return tuple(outcomes), diagnostics


def _perform(session: Session, action: LifecycleAction, hooks: ChartTestHookRunner) -> None:
    entry = action.entry
    if action.kind is ActionKind.NAMESPACE_ENSURE:
        session.kubectl.create_namespace(entry.namespace)
    elif action.kind is ActionKind.HELM_LINT:
        ensure_dependencies(session.helm, entry.chart.path)
        session.helm.lint(entry.chart.path, list(entry.values))
    elif action.kind is ActionKind.INSTALL:
        converge(session, release(entry, sets={}))
    elif action.kind is ActionKind.WORKLOAD_READY:
        wait(session, release(entry, sets={}))
    elif action.kind is ActionKind.HELM_TEST:
        result = session.helm.test(
            entry.chart.name, namespace=entry.namespace, timeout=parse_duration(entry.spec.timeout)
        )
        if result.returncode != 0:
            output = (result.stderr or result.stdout).strip()
            raise ChartManagerError(
                f"helm test exited {result.returncode}" + (f": {output}" if output else "")
            )
    else:
        hooks.run(action)


def _diagnostics(session: Session, action: LifecycleAction, exc: ChartManagerError) -> str:
    if isinstance(exc, ReleaseFailed):
        return exc.diagnostics
    try:
        return session.kubectl.diagnostics(action.entry.namespace)
    except ChartManagerError as error:
        _LOG.warning("namespace diagnostics unavailable: %s", error)
        return ""


def _subject(action: LifecycleAction) -> str:
    entry = action.entry
    return f"{entry.chart.name}:{entry.profile} in {entry.namespace}"
