"""Compile `chart test` plans from authored ChartLifecycle profiles.

Each chart in dependency order gets: its namespace, an optional lint, the preInstall hook,
the install (`converge`), the postInstall hook, an optional `helm test`, and its cleanup
hook, which the plan moves to its tail. The namespace comes first because a preInstall
hook may write into it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from chart_manager.commands.test.models import (
    ActionKind,
    ActionTarget,
    ChartTestRequest,
    LifecycleAction,
    LifecyclePlan,
    PlanError,
)
from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.paths import validate_hook_executable
from chart_manager.shared.charts.cluster_tests import ClusterTestCatalog
from chart_manager.shared.charts.install_plan import DependencyResolver
from chart_manager.shared.charts.lifecycle import require_cluster_test_profile
from chart_manager.shared.cluster.bootstrap import ExternallySatisfiedLifecycle

#: Changing this string changes every `action_id` and therefore every `input_digest`.
_CLUSTER_TEST_PREFIX = "cluster-test"

EXTERNAL_BOOTSTRAP_WARNING_PREFIX = "environment bootstrap externally satisfies chart(s): "
SKIPPED_REQUIRES_WARNING_PREFIX = "requires assumed installed (--skip-requires): "


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
    skipped: tuple[SkippedRequirement, ...] = ()


def compile_plan(
    request: ChartTestRequest,
    *,
    root: Path,
    catalog: ClusterTestCatalog,
    bootstrap_owned: Iterable[ExternallySatisfiedLifecycle],
) -> CompiledPlan:
    """The plan for `request`: its chart and any dependent tests, minus what bootstrap owns."""
    resolver = DependencyResolver(catalog.get)
    requested = [(request.chart, request.profile)]
    if request.include_dependent_tests:
        requested.extend(
            (ref.chart, ref.profile) for ref in resolver.dependent_tests(request.chart)
        )
    owned = frozenset(bootstrap_owned)
    plans = [
        exclude_bootstrap_owned_charts(
            compile_cluster_test(
                chart,
                profile,
                root=root,
                catalog=catalog,
                resolver=resolver,
                namespace_override=request.namespace,
                lint=request.lint,
            ),
            owned,
            root=root,
        )
        for chart, profile in requested
    ]
    plan = merge_plans(plans)
    if not request.skip_requires:
        return CompiledPlan(plan)
    return exclude_required_lifecycles(plan, requested)


def compile_cluster_test(
    chart: str,
    profile: str,
    *,
    root: Path,
    catalog: ClusterTestCatalog,
    resolver: DependencyResolver,
    namespace_override: str | None = None,
    lint: bool = False,
) -> LifecyclePlan:
    """Compile one chart:profile and everything it requires, dependencies first."""
    actions: list[LifecycleAction] = []
    for entry in resolver.install_plan(chart, profile):
        cluster_chart = catalog.get(entry.chart)
        profile_spec = require_cluster_test_profile(cluster_chart.spec, entry.profile)
        values = tuple(path.resolve() for path in catalog.value_paths(cluster_chart, entry.profile))
        is_requested_target = entry.chart == chart and entry.profile == profile
        namespace = (
            namespace_override
            if is_requested_target and namespace_override is not None
            else profile_spec.namespace
        )
        target = ActionTarget(
            chart=entry.chart, profile=entry.profile, release=entry.chart, namespace=namespace
        )
        prefix = (_CLUSTER_TEST_PREFIX, entry.chart, entry.profile)

        def action(
            kind: ActionKind,
            *,
            action_values: tuple[Path, ...] = (),
            timeout: str | None = None,
            metadata: tuple[tuple[str, str], ...] = (),
            path: Path = cluster_chart.path,
            prefix: tuple[str, ...] = prefix,
            target: ActionTarget = target,
        ) -> LifecycleAction:
            action_id = _action_id(*prefix, kind)
            return LifecycleAction(
                action_id=action_id,
                kind=kind,
                target=target,
                input_digest=_input_digest(
                    root=root,
                    action_id=action_id,
                    chart_path=path,
                    values=action_values,
                    metadata=metadata,
                ),
                chart_path=path.resolve(),
                values=action_values,
                timeout=timeout,
            )

        hooks = profile_spec.hooks
        hook_actions = {
            kind: _hook_action(
                kind,
                tuple(argv),
                root=root,
                target=target,
                prefix=prefix,
                chart_path=cluster_chart.path,
                timeout=profile_spec.timeout,
                field=f"{entry.chart}: spec.clusterTest.profiles.{entry.profile}.hooks.{phase}[0]",
            )
            for kind, phase, argv in (
                (ActionKind.HOOK_PRE_INSTALL, "preInstall", hooks and hooks.pre_install),
                (ActionKind.HOOK_POST_INSTALL, "postInstall", hooks and hooks.post_install),
                (ActionKind.HOOK_CLEANUP, "cleanup", hooks and hooks.cleanup),
            )
            if argv
        }
        timed = (("timeout", profile_spec.timeout),)
        actions.append(action(ActionKind.NAMESPACE_ENSURE))
        if lint:
            actions.append(action(ActionKind.HELM_LINT, action_values=values))
        if pre_install := hook_actions.get(ActionKind.HOOK_PRE_INSTALL):
            actions.append(pre_install)
        actions.append(
            action(ActionKind.INSTALL, action_values=values, timeout=profile_spec.timeout)
        )
        if post_install := hook_actions.get(ActionKind.HOOK_POST_INSTALL):
            actions.append(post_install)
        if profile_spec.helm_test:
            actions.append(
                action(
                    ActionKind.HELM_TEST,
                    action_values=values,
                    timeout=profile_spec.timeout,
                    metadata=timed,
                )
            )
        if cleanup := hook_actions.get(ActionKind.HOOK_CLEANUP):
            actions.append(cleanup)
    return LifecyclePlan(chart=chart, profile=profile, actions=cleanup_tail(actions))


def _hook_action(
    kind: ActionKind,
    command: tuple[str, ...],
    *,
    root: Path,
    target: ActionTarget,
    prefix: tuple[str, ...],
    chart_path: Path,
    timeout: str,
    field: str,
) -> LifecycleAction:
    """Compile one hook; a repository script is digested like a values file."""
    script = validate_hook_executable(root, command[0], field=field, require_on_path=True)
    action_id = _action_id(*prefix, kind)
    return LifecycleAction(
        action_id=action_id,
        kind=kind,
        target=target,
        input_digest=_input_digest(
            root=root,
            action_id=action_id,
            chart_path=chart_path,
            values=(),
            metadata=(),
            command=command,
            script=script,
        ),
        chart_path=chart_path.resolve(),
        timeout=timeout,
        command=command,
    )


def exclude_bootstrap_owned_charts(
    plan: LifecyclePlan, bootstrap_owned: frozenset[ExternallySatisfiedLifecycle], *, root: Path
) -> LifecyclePlan:
    """Drop the work for charts bootstrap already installed.

    A required chart bootstrap owns is dropped entirely. When bootstrap owns the
    requested chart itself, its install becomes a readiness wait and its helm test
    stays, so the requested profile is still checked.
    """
    if any(
        not identity.chart.strip() or not identity.profile.strip() or not identity.namespace.strip()
        for identity in bootstrap_owned
    ):
        raise PlanError("bootstrap lifecycle identity fields must not be empty")

    def owned(action: LifecycleAction) -> bool:
        target = action.target
        if target.profile is None or target.namespace is None:
            return False
        return (
            ExternallySatisfiedLifecycle(
                chart_path=action.chart_path.resolve(),
                chart=target.chart,
                profile=target.profile,
                namespace=target.namespace,
            )
            in bootstrap_owned
        )

    kept: list[LifecycleAction] = []
    removed: set[str] = set()
    for action in plan.actions:
        if not owned(action):
            kept.append(action)
            continue
        is_target = action.target.chart == plan.chart and action.target.profile == plan.profile
        if is_target and action.kind is ActionKind.HELM_TEST:
            kept.append(action)
        elif is_target and action.kind is ActionKind.INSTALL:
            action_id = _action_id(
                _CLUSTER_TEST_PREFIX, plan.chart, plan.profile, ActionKind.WORKLOAD_READY
            )
            digest = _input_digest(
                root=root,
                action_id=action_id,
                chart_path=action.chart_path,
                values=action.values,
                metadata=(("timeout", action.timeout or ""),),
            )
            kept.append(
                replace(
                    action,
                    kind=ActionKind.WORKLOAD_READY,
                    action_id=action_id,
                    input_digest=digest,
                )
            )
            removed.add(action.target.chart)
        else:
            removed.add(action.target.chart)
    if not removed:
        return plan
    warning = (
        EXTERNAL_BOOTSTRAP_WARNING_PREFIX
        + ", ".join(sorted(removed))
        + "; environment-owned preparation/install actions were excluded "
        "from this executable plan"
    )
    return replace(plan, actions=tuple(kept), warnings=(*plan.warnings, warning))


def exclude_required_lifecycles(
    plan: LifecyclePlan, requested: Iterable[tuple[str, str]]
) -> CompiledPlan:
    """Remove every action that belongs only to a `requires` entry (`--skip-requires`).

    A chart selected by `--dependent-tests` still runs in full even when another
    selected chart also requires it.
    """
    selected = frozenset(requested)
    if not selected or any(not chart.strip() or not profile.strip() for chart, profile in selected):
        raise PlanError("requested lifecycle identity fields must not be empty")

    skipped: dict[tuple[str, str], SkippedRequirement] = {}
    for action in plan.actions:
        profile = action.target.profile
        if profile is None or (action.target.chart, profile) in selected:
            continue
        release, namespace = action.target.release, action.target.namespace
        if not release or not namespace:
            raise PlanError(
                f"required lifecycle {action.target.chart}:{profile} "
                "must identify a release and namespace"
            )
        candidate = SkippedRequirement(action.target.chart, profile, release, namespace)
        previous = skipped.setdefault((action.target.chart, profile), candidate)
        if previous != candidate:
            raise PlanError(
                f"required lifecycle {action.target.chart}:{profile} has conflicting coordinates"
            )
    if not skipped:
        return CompiledPlan(plan)
    actions = tuple(
        action
        for action in plan.actions
        if (action.target.chart, action.target.profile) not in skipped
    )
    warning = SKIPPED_REQUIRES_WARNING_PREFIX + ", ".join(
        f"{s.chart}:{s.profile}" for s in skipped.values()
    )
    return CompiledPlan(
        replace(plan, actions=actions, warnings=(*plan.warnings, warning)),
        tuple(skipped.values()),
    )


def without_required_helm_tests(
    plan: LifecyclePlan, requested: Iterable[tuple[str, str]]
) -> LifecyclePlan:
    """Keep installing prerequisites on a new cluster, but helm-test selected charts only."""
    selected = frozenset(requested)
    return replace(
        plan,
        actions=tuple(
            action
            for action in plan.actions
            if action.kind is not ActionKind.HELM_TEST
            or (action.target.chart, action.target.profile) in selected
        ),
    )


def merge_plans(plans: list[LifecyclePlan]) -> LifecyclePlan:
    """Compose the requested charts' plans, running each chart:profile action once.

    Identical actions shared by several plans are kept once in first-seen order; the
    cleanups then form one tail.
    """
    if not plans:
        raise PlanError("cluster test produced no lifecycle plans")
    actions: dict[str, LifecycleAction] = {}
    warnings: dict[str, None] = {}
    for plan in plans:
        for action in plan.actions:
            previous = actions.setdefault(action.action_id, action)
            if previous != action:
                raise PlanError(
                    f"conflicting lifecycle action {action.action_id!r} across test plans"
                )
        warnings.update(dict.fromkeys(plan.warnings))
    return replace(plans[0], actions=cleanup_tail(actions.values()), warnings=tuple(warnings))


def _action_id(*parts: object) -> str:
    """Build a deterministic, evidence-path-safe human-readable action ID."""
    candidate = ".".join(
        str(part.value if isinstance(part, ActionKind) else part) for part in parts
    )
    if len(candidate) <= 128 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", candidate):
        return candidate
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", candidate)
    suffix = hashlib.sha256(candidate.encode()).hexdigest()[:16]
    return f"{safe[:111]}.{suffix}"


def _clean_metadata(metadata: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """Omit empty optional values from projections and digest inputs."""
    return tuple((key, value) for key, value in metadata if value)


def _input_digest(
    *,
    root: Path,
    action_id: str,
    chart_path: Path,
    values: tuple[Path, ...],
    metadata: tuple[tuple[str, str], ...],
    command: tuple[str, ...] = (),
    script: Path | None = None,
) -> str:
    """Digest action intent and the local authored files that determine it."""
    root = root.resolve()
    digest = hashlib.sha256()
    digest.update(action_id.encode())
    digest.update(b"\0")
    for key, value in sorted(_clean_metadata(metadata)):
        digest.update(key.encode())
        digest.update(b"=")
        digest.update(value.encode())
        digest.update(b"\0")
    # Empty for non-hook actions, so their digests are unchanged.
    for arg in command:
        digest.update(b"argv=")
        digest.update(arg.encode())
        digest.update(b"\0")
    # The top-level ``charts/`` directory contains generated/downloaded Helm
    # dependency artifacts. ``helm dependency update`` is allowed to create
    # or replace those without making the just-compiled plan instantly stale;
    # Chart.yaml and Chart.lock capture the authored dependency intent.
    candidates: set[Path] = set()
    for path in chart_path.rglob("*"):
        if path.relative_to(chart_path).parts[0] == "charts":
            continue
        if path.is_symlink():
            _resolve_digest_input(path, root)
        if path.is_file():
            candidates.add(_resolve_digest_input(path, root))
    candidates.update(_resolve_digest_input(path, root) for path in values)
    if script is not None:
        candidates.add(_resolve_digest_input(script, root))
    for resolved in sorted(candidates):
        label = str(resolved.relative_to(root))
        digest.update(label.encode())
        digest.update(b"\0")
        if resolved.is_file():
            digest.update(b"file\0")
            digest.update(resolved.read_bytes())
        elif resolved.is_dir():
            digest.update(b"directory")
        else:
            digest.update(b"missing")
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _resolve_digest_input(path: Path, root: Path) -> Path:
    """Resolve a digest input and reject symlinks escaping the repository."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise SpecError(f"digest input escapes repository root: {path} resolves to {resolved}")
    return resolved


def cleanup_tail(actions: Iterable[LifecycleAction]) -> tuple[LifecycleAction, ...]:
    """Move hook-cleanup actions to the end, in reverse install order.

    Dependents clean up before their dependencies; other actions keep their order.
    """
    ordered = tuple(actions)
    first_seen: dict[tuple[str, str | None], int] = {}
    for action in ordered:
        first_seen.setdefault((action.target.chart, action.target.profile), len(first_seen))
    cleanups = sorted(
        (action for action in ordered if action.kind is ActionKind.HOOK_CLEANUP),
        key=lambda action: first_seen[(action.target.chart, action.target.profile)],
        reverse=True,
    )
    return (
        *(action for action in ordered if action.kind is not ActionKind.HOOK_CLEANUP),
        *cleanups,
    )
