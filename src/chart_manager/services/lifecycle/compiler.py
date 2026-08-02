"""Compile an authored ChartLifecycle cluster-test capability into an ordered action plan."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from chart_manager.domain.cluster_tests import ClusterTestCatalog
from chart_manager.domain.install_plan import DependencyResolver
from chart_manager.domain.lifecycle_policy import require_cluster_test_profile
from chart_manager.plumbing.errors import SpecError
from chart_manager.services.lifecycle.models import (
    ActionKind,
    ActionTarget,
    LifecycleAction,
    LifecyclePlan,
)
from chart_manager.settings import DEFAULT_CHARTS_DIR

#: Frozen first segment of every action ID (formerly `Workflow.CLUSTER_TEST`,
#: deleted as a single-member enum). Changing this string changes every
#: `action_id` and therefore every `input_digest`.
_CLUSTER_TEST_PREFIX = "cluster-test"


class ClusterTestCompiler:
    """Compile a chart's authored cluster-test capability into an ordered action plan."""

    def __init__(
        self,
        root: Path,
        *,
        charts_dir: Path = DEFAULT_CHARTS_DIR,
        cluster_tests: ClusterTestCatalog | None = None,
        resolver: DependencyResolver | None = None,
    ) -> None:
        """Anchor all chart and value resolution at the repository root.

        `cluster_tests` and `resolver` are constructor seams so a caller that
        already owns those repository seams shares them instead of reaching in
        and reassigning the attributes afterwards -- a fourth seam added here
        would then leave such a call site silently holding a stale one.
        """
        self.root = root.resolve()
        self.charts_dir = charts_dir
        self.cluster_tests = cluster_tests or ClusterTestCatalog(self.root, charts_dir=charts_dir)
        self.resolver = resolver or DependencyResolver(self.cluster_tests.get)

    def compile_cluster_test(
        self,
        chart: str,
        profile: str,
        *,
        default_namespace: str,
        namespace_override: str | None = None,
        lint: bool = False,
    ) -> LifecyclePlan:
        """Compile a dependency-first live cluster-test action plan.

        Cluster creation, API readiness, and LocalCluster bootstrap
        intentionally remain outside chart-authored intent and therefore
        outside this chart plan.
        """
        install_plan = self.resolver.install_plan(chart, profile)
        actions: list[LifecycleAction] = []

        for entry in install_plan:
            cluster_chart = self.cluster_tests.get(entry.chart)
            profile_spec = require_cluster_test_profile(cluster_chart.spec, entry.profile)
            values = tuple(
                path.resolve()
                for path in self.cluster_tests.value_paths(cluster_chart, entry.profile)
            )
            is_requested_target = entry.chart == chart and entry.profile == profile
            namespace = (
                namespace_override
                if is_requested_target and namespace_override is not None
                else profile_spec.namespace or default_namespace
            )
            target_coordinates = ActionTarget(
                chart=entry.chart,
                profile=entry.profile,
                release=entry.chart,
                namespace=namespace,
            )
            prefix = (_CLUSTER_TEST_PREFIX, entry.chart, entry.profile)
            entry_actions: list[LifecycleAction] = []
            for kind in (
                ActionKind.NAMESPACE_ENSURE,
                ActionKind.HELM_DEPENDENCY_UPDATE,
                *((ActionKind.HELM_LINT,) if lint else ()),
                ActionKind.HELM_UPGRADE_INSTALL,
            ):
                action_id = _action_id(*prefix, kind)
                action_values = (
                    values
                    if kind in (ActionKind.HELM_LINT, ActionKind.HELM_UPGRADE_INSTALL)
                    else ()
                )
                entry_actions.append(
                    LifecycleAction(
                        action_id=action_id,
                        kind=kind,
                        target=target_coordinates,
                        input_digest=_input_digest(
                            root=self.root,
                            action_id=action_id,
                            chart_path=cluster_chart.path,
                            values=action_values,
                            metadata=(),
                        ),
                        chart_path=cluster_chart.path.resolve(),
                        values=action_values,
                        timeout=(
                            profile_spec.timeout
                            if kind is ActionKind.HELM_UPGRADE_INSTALL
                            else None
                        ),
                    )
                )
            actions.extend(entry_actions)

            ready_id = _action_id(*prefix, ActionKind.WORKLOAD_READY)
            ready = LifecycleAction(
                action_id=ready_id,
                kind=ActionKind.WORKLOAD_READY,
                target=target_coordinates,
                input_digest=_input_digest(
                    root=self.root,
                    action_id=ready_id,
                    chart_path=cluster_chart.path,
                    values=values,
                    metadata=(("timeout", profile_spec.timeout),),
                ),
                chart_path=cluster_chart.path.resolve(),
                values=values,
                timeout=profile_spec.timeout,
            )
            actions.append(ready)
            if profile_spec.helm_test:
                test_id = _action_id(*prefix, ActionKind.HELM_TEST)
                helm_test = LifecycleAction(
                    action_id=test_id,
                    kind=ActionKind.HELM_TEST,
                    target=target_coordinates,
                    input_digest=_input_digest(
                        root=self.root,
                        action_id=test_id,
                        chart_path=cluster_chart.path,
                        values=values,
                        metadata=(("timeout", profile_spec.timeout),),
                    ),
                    chart_path=cluster_chart.path.resolve(),
                    values=values,
                    timeout=profile_spec.timeout,
                )
                actions.append(helm_test)

        return LifecyclePlan(
            chart=chart,
            profile=profile,
            actions=tuple(actions),
        )


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
