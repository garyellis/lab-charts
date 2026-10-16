"""Compile `chart test` plans from authored ChartLifecycle profiles.

Each chart in dependency order gets: its namespace, an optional lint, the preInstall hook,
the install (`converge`), the postInstall hook, an optional `helm test`, and its cleanup
hook, which the plan moves to its tail. The namespace comes first because a preInstall
hook may write into it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from chart_manager.commands.test.models import (
    ActionKind,
    ChartTestRequest,
    LifecycleAction,
    LifecyclePlan,
    PlanError,
)
from chart_manager.plumbing.paths import validate_hook_executable
from chart_manager.shared.charts.chart import load_chart
from chart_manager.shared.charts.install_plan import InstallPlanEntry, install_plan
from chart_manager.shared.charts.lifecycle import require_chart_test
from chart_manager.shared.cluster.bootstrap import ExternallySatisfiedLifecycle

EXTERNAL_BOOTSTRAP_WARNING_PREFIX = "environment bootstrap externally satisfies chart(s): "
SKIPPED_REQUIRES_WARNING_PREFIX = "requires assumed installed (--skip-requires): "

#: What happens to required entries: installed and tested, left out (`--skip-requires`),
#: or installed without a helm test (`--skip-requires` on a new cluster).
Requires = Literal["install", "skip", "install-untested"]


@dataclass(frozen=True)
class SkippedRequirement:
    """A required release `--skip-requires` left out of the plan; it must be installed."""

    chart: str
    profile: str
    release: str
    namespace: str


@dataclass(frozen=True)
class CompiledPlan:
    """The plan to run, and the requirements a real run must find installed."""

    plan: LifecyclePlan
    skipped: tuple[SkippedRequirement, ...]


def compile_plan(
    request: ChartTestRequest,
    *,
    root: Path,
    charts_dir: Path,
    bootstrap_owned: frozenset[ExternallySatisfiedLifecycle],
    requires: Requires,
) -> CompiledPlan:
    """The plan for `request`: its chart, any dependent tests, and what they require.

    The requested install plans merge into one entry list, dependencies first, each
    chart:profile once. An entry is *selected* (the requested chart or a dependent test)
    or *required*, and gets its steps from this table:

    | Entry    | install   | skip                 | install-untested | bootstrap owns it    |
    |----------|-----------|----------------------|------------------|----------------------|
    | selected | all steps | all steps            | all steps        | workload-ready, then |
    |          |           |                      |                  | its helm test        |
    | required | all steps | left out, warned, a  | all steps but    | left out, warned     |
    |          |           | SkippedRequirement   | the helm test    |                      |

    `request.namespace` relocates each selected chart, never what it requires; a
    chart:profile that resolves to two namespaces is a `PlanError`. Cleanup hooks
    run last, in reverse entry order.
    """
    requested = [(request.chart, request.profile)]
    if request.include_dependent_tests:
        chart = load_chart(charts_dir / request.chart)
        spec = require_chart_test(chart.lifecycle, chart_name=chart.name)
        requested.extend((ref.chart, ref.profile) for ref in spec.dependent_tests)
    selected = frozenset(requested)
    entries: dict[tuple[str, str], InstallPlanEntry] = {}
    for chart_name, profile in requested:
        plan_entries = install_plan(charts_dir, chart_name, profile)
        if request.namespace is not None:
            plan_entries[-1] = replace(plan_entries[-1], namespace=request.namespace)
        for entry in plan_entries:
            first = entries.setdefault((entry.chart.name, entry.profile), entry)
            if first.namespace != entry.namespace:
                raise PlanError(
                    f"{entry.chart.name}:{entry.profile} resolves to two namespaces "
                    f"({first.namespace}, {entry.namespace})"
                )

    actions: list[LifecycleAction] = []
    cleanups: list[LifecycleAction] = []
    owned: set[str] = set()
    skipped: list[SkippedRequirement] = []
    for key, entry in entries.items():
        if ExternallySatisfiedLifecycle.of(entry) in bootstrap_owned:
            owned.add(entry.chart.name)
            if key in selected:
                actions.append(LifecycleAction(entry, ActionKind.WORKLOAD_READY))
                if entry.spec.helm_test:
                    actions.append(LifecycleAction(entry, ActionKind.HELM_TEST))
        elif key in selected or requires == "install":
            _append_steps(entry, actions, cleanups, root=root, lint=request.lint, test=True)
        elif requires == "install-untested":
            _append_steps(entry, actions, cleanups, root=root, lint=request.lint, test=False)
        else:
            skipped.append(SkippedRequirement(*key, entry.chart.name, entry.namespace))

    warnings = []
    if owned:
        warnings.append(
            EXTERNAL_BOOTSTRAP_WARNING_PREFIX
            + ", ".join(sorted(owned))
            + "; environment-owned preparation/install actions were excluded "
            "from this executable plan"
        )
    if skipped:
        warnings.append(
            SKIPPED_REQUIRES_WARNING_PREFIX + ", ".join(f"{s.chart}:{s.profile}" for s in skipped)
        )
    plan = LifecyclePlan(
        chart=request.chart,
        profile=request.profile,
        actions=(*actions, *reversed(cleanups)),
        warnings=tuple(warnings),
    )
    return CompiledPlan(plan, tuple(skipped))


def _append_steps(
    entry: InstallPlanEntry,
    actions: list[LifecycleAction],
    cleanups: list[LifecycleAction],
    *,
    root: Path,
    lint: bool,
    test: bool,
) -> None:
    """Add one entry's steps: namespace, lint, hooks around its install, then its helm test."""
    hooks = entry.spec.hooks
    hook_actions = {
        kind: _hook_action(entry, kind, phase, argv, root=root)
        for kind, phase, argv in (
            (ActionKind.HOOK_PRE_INSTALL, "preInstall", hooks and hooks.pre_install),
            (ActionKind.HOOK_POST_INSTALL, "postInstall", hooks and hooks.post_install),
            (ActionKind.HOOK_CLEANUP, "cleanup", hooks and hooks.cleanup),
        )
        if argv
    }
    actions.append(LifecycleAction(entry, ActionKind.NAMESPACE_ENSURE))
    if lint:
        actions.append(LifecycleAction(entry, ActionKind.HELM_LINT))
    if pre_install := hook_actions.get(ActionKind.HOOK_PRE_INSTALL):
        actions.append(pre_install)
    actions.append(LifecycleAction(entry, ActionKind.INSTALL))
    if post_install := hook_actions.get(ActionKind.HOOK_POST_INSTALL):
        actions.append(post_install)
    if test and entry.spec.helm_test:
        actions.append(LifecycleAction(entry, ActionKind.HELM_TEST))
    if cleanup := hook_actions.get(ActionKind.HOOK_CLEANUP):
        cleanups.append(cleanup)


def _hook_action(
    entry: InstallPlanEntry, kind: ActionKind, phase: str, argv: list[str], *, root: Path
) -> LifecycleAction:
    """Compile one hook after checking its executable."""
    field = f"{entry.chart.name}: spec.chartTest.profiles.{entry.profile}.hooks.{phase}[0]"
    validate_hook_executable(root, argv[0], field=field, require_on_path=True)
    return LifecycleAction(entry, kind, command=tuple(argv))
