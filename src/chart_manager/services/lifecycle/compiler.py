"""Compile authored ChartLifecycle resources into lifecycle action DAGs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from chart_manager.plumbing.errors import SpecError
from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.domain.install_plan import DependencyResolver
from chart_manager.services.lifecycle.models import (
    ActionKind,
    ActionTarget,
    EdgeKind,
    LifecycleAction,
    LifecycleEdge,
    LifecyclePlan,
    Workflow,
)
from chart_manager.services.manifest_validation.catalog import (
    load_manifest_validation_target,
)
from chart_manager.services.manifest_validation.compiler import (
    resolve_manifest_validation,
)
from chart_manager.services.manifest_validation.validator_registry import (
    VALIDATOR_REGISTRY,
)
from chart_manager.services.manifest_validation.validators import (
    ValidatorCategory,
    ValidatorProvider,
    validate_registry,
)
from chart_manager.settings import DEFAULT_CHARTS_DIR


class LifecycleCompiler:
    """Compile authored lifecycle capabilities into a common typed action graph."""

    def __init__(
        self,
        root: Path,
        *,
        charts_dir: Path = DEFAULT_CHARTS_DIR,
        validator_providers: tuple[ValidatorProvider, ...] = VALIDATOR_REGISTRY,
    ) -> None:
        """Anchor all chart and value resolution at the repository root."""
        self.root = root.resolve()
        self.charts_dir = charts_dir
        self.cluster_tests = ClusterTestCatalog(self.root, charts_dir=charts_dir)
        self.resolver = DependencyResolver(self.cluster_tests.get)
        self.validator_providers = validate_registry(validator_providers)

    def compile_validation(self, chart: str, environment: str) -> LifecyclePlan:
        """Compile static validation for one chart and authored environment."""
        target = load_manifest_validation_target(
            self.root,
            chart,
            charts_dir=self.charts_dir,
        )
        resolved = resolve_manifest_validation(
            target,
            self.root,
            providers=self.validator_providers,
        )
        try:
            selected = resolved.environments[environment]
        except KeyError as exc:
            available = ", ".join(sorted(resolved.environments))
            raise SpecError(
                f"unknown environment {environment!r} for chart {chart!r}; "
                f"available environments: {available}"
            ) from exc

        coordinates = ActionTarget(
            workflow=Workflow.VALIDATION,
            chart=target.name,
            environment=environment,
            release=target.spec.release_name,
            namespace=selected.namespace,
        )
        execution_inputs = _validation_execution_inputs(self.root)
        definitions = [
            (
                ActionKind.HELM_DEPENDENCY_UPDATE,
                execution_inputs,
                (("helmVersion", target.spec.helm_version or ""),),
                execution_inputs,
            ),
            (
                ActionKind.RENDER,
                selected.values,
                (
                    ("helmVersion", target.spec.helm_version or ""),
                    ("helmBinary", target.spec.helm_binary or ""),
                ),
                (),
            ),
        ]
        enabled_invocations = tuple(
            invocation
            for invocation in resolved.validator_invocations
            if invocation.enabled
        )
        for invocation in enabled_invocations:
            definitions.append(
                (
                    ActionKind(invocation.lifecycle_action_kind),
                    selected.values,
                    invocation.lifecycle_metadata,
                    (*execution_inputs, *invocation.lifecycle_additional_paths),
                )
            )
        actions: list[LifecycleAction] = []
        for kind, digest_values, metadata, additional_paths in definitions:
            action_id = _action_id(
                Workflow.VALIDATE,
                target.name,
                environment,
                kind,
            )
            actions.append(
                LifecycleAction(
                    action_id=action_id,
                    kind=kind,
                    input_digest=_input_digest(
                        root=self.root,
                        action_id=action_id,
                        chart_path=target.path,
                        values=digest_values,
                        metadata=metadata,
                        additional_paths=additional_paths,
                    ),
                    chart_path=target.path.resolve(),
                    target=coordinates,
                    values=selected.values,
                    metadata=_clean_metadata(metadata),
                )
            )

        edges: list[LifecycleEdge] = [
            LifecycleEdge(
                actions[0].action_id,
                actions[1].action_id,
                EdgeKind.SEQUENCE,
                "chart dependencies must be current before rendering",
            ),
        ]
        validator_actions = tuple(zip(actions[2:], enabled_invocations, strict=True))
        schema_actions = tuple(
            action
            for action, invocation in validator_actions
            if invocation.category is ValidatorCategory.SCHEMA
        )
        policy_actions = tuple(
            action
            for action, invocation in validator_actions
            if invocation.category is ValidatorCategory.POLICY
        )
        for action in schema_actions or policy_actions:
            edges.append(
                LifecycleEdge(
                    actions[1].action_id,
                    action.action_id,
                    EdgeKind.INPUT,
                    "validation consumes rendered manifests",
                )
            )
        for predecessor in schema_actions:
            for successor in policy_actions:
                edges.append(
                    LifecycleEdge(
                        predecessor.action_id,
                        successor.action_id,
                        EdgeKind.SEQUENCE,
                        "preserve the authored validation phase order",
                    )
                )
        return LifecyclePlan(
            workflow=Workflow.VALIDATE,
            chart=target.name,
            environment=environment,
            actions=tuple(actions),
            edges=tuple(edges),
            warnings=resolved.warnings,
        )

    def compile_cluster_test(
        self,
        chart: str,
        profile: str,
        *,
        default_namespace: str = "default",
    ) -> LifecyclePlan:
        """Compile a dependency-first live cluster-test action DAG.

        Kind creation, API readiness, and environment-owned bootstrap (notably
        Cilium's live control-plane addressing) intentionally remain outside
        chart-authored intent and therefore outside this chart plan.
        """
        install_plan = self.resolver.install_plan(chart, profile)
        actions: list[LifecycleAction] = []
        edges: list[LifecycleEdge] = []
        final_by_key: dict[tuple[str, str], str] = {}
        install_by_key: dict[tuple[str, str], str] = {}

        for entry in install_plan:
            cluster_chart = self.cluster_tests.get(entry.chart)
            profile_spec = cluster_chart.spec.profile(entry.profile)
            values = tuple(
                path.resolve()
                for path in self.cluster_tests.value_paths(cluster_chart, entry.profile)
            )
            namespace = profile_spec.namespace or default_namespace
            target_coordinates = ActionTarget(
                workflow=Workflow.CLUSTER_TEST,
                chart=entry.chart,
                profile=entry.profile,
                release=entry.chart,
                namespace=namespace,
            )
            prefix = (Workflow.CLUSTER_TEST, entry.chart, entry.profile)
            entry_actions: list[LifecycleAction] = []
            for kind in (
                ActionKind.NAMESPACE_ENSURE,
                ActionKind.HELM_DEPENDENCY_UPDATE,
                ActionKind.HELM_UPGRADE_INSTALL,
            ):
                action_id = _action_id(*prefix, kind)
                action_values = values if kind is ActionKind.HELM_UPGRADE_INSTALL else ()
                metadata = (("target", str(entry.target).lower()),)
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
                            metadata=metadata,
                        ),
                        chart_path=cluster_chart.path.resolve(),
                        values=action_values,
                        timeout=(
                            profile_spec.timeout
                            if kind is ActionKind.HELM_UPGRADE_INSTALL
                            else None
                        ),
                        metadata=metadata,
                    )
                )
            actions.extend(entry_actions)
            namespace_action, dependency_action, install_action = entry_actions
            install_by_key[(entry.chart, entry.profile)] = install_action.action_id
            edges.extend(
                (
                    LifecycleEdge(
                        namespace_action.action_id,
                        install_action.action_id,
                        EdgeKind.SEQUENCE,
                        "release namespace must exist before installation",
                    ),
                    LifecycleEdge(
                        dependency_action.action_id,
                        install_action.action_id,
                        EdgeKind.SEQUENCE,
                        "Helm dependencies must be current before installation",
                    ),
                )
            )

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
                metadata=(("target", str(entry.target).lower()),),
            )
            actions.append(ready)
            edges.append(
                LifecycleEdge(
                    install_action.action_id,
                    ready.action_id,
                    EdgeKind.SEQUENCE,
                    "installed workloads must become ready",
                )
            )
            final_action = ready
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
                        metadata=(
                            ("timeout", profile_spec.timeout),
                            (
                                "checks",
                                json.dumps(
                                    [
                                        {
                                            "name": check.name,
                                            "type": check.type,
                                            "description": check.description,
                                        }
                                        for check in profile_spec.effective_checks()
                                    ],
                                    sort_keys=True,
                                ),
                            ),
                        ),
                    ),
                    chart_path=cluster_chart.path.resolve(),
                    values=values,
                    timeout=profile_spec.timeout,
                    metadata=(
                        ("target", str(entry.target).lower()),
                        (
                            "checks",
                            json.dumps([check.name for check in profile_spec.effective_checks()]),
                        ),
                    ),
                )
                actions.append(helm_test)
                edges.append(
                    LifecycleEdge(
                        ready.action_id,
                        helm_test.action_id,
                        EdgeKind.SEQUENCE,
                        "Helm tests run against ready workloads",
                    )
                )
                final_action = helm_test
            final_by_key[(entry.chart, entry.profile)] = final_action.action_id

        # Encode the authored runtime graph explicitly rather than relying on
        # the tuple order inherited from the dependency resolver.
        for entry in install_plan:
            cluster_chart = self.cluster_tests.get(entry.chart)
            profile_spec = cluster_chart.spec.profile(entry.profile)
            dependent_install = install_by_key[(entry.chart, entry.profile)]
            for required in profile_spec.requires:
                required_final = final_by_key[(required.chart, required.profile)]
                edges.append(
                    LifecycleEdge(
                        required_final,
                        dependent_install,
                        EdgeKind.RUNTIME_REQUIREMENT,
                        f"{entry.chart}:{entry.profile} requires "
                        f"{required.chart}:{required.profile}",
                    )
                )

        return LifecyclePlan(
            workflow=Workflow.CLUSTER_TEST,
            chart=chart,
            profile=profile,
            actions=tuple(actions),
            edges=tuple(edges),
        )


def compile_validation_plan(
    root: Path,
    chart: str,
    environment: str,
    *,
    charts_dir: Path = DEFAULT_CHARTS_DIR,
) -> LifecyclePlan:
    """Convenience wrapper for one validation plan."""
    return LifecycleCompiler(
        root,
        charts_dir=charts_dir,
    ).compile_validation(chart, environment)


def compile_cluster_test_plan(
    root: Path,
    chart: str,
    profile: str,
    *,
    default_namespace: str = "default",
    charts_dir: Path = DEFAULT_CHARTS_DIR,
) -> LifecyclePlan:
    """Convenience wrapper for one cluster-test plan."""
    return LifecycleCompiler(root, charts_dir=charts_dir).compile_cluster_test(
        chart,
        profile,
        default_namespace=default_namespace,
    )


def _action_id(*parts: object) -> str:
    """Build a deterministic, evidence-path-safe human-readable action ID."""
    candidate = ".".join(
        str(part.value if isinstance(part, (Workflow, ActionKind)) else part) for part in parts
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
    additional_paths: tuple[Path, ...] = (),
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
    for additional in additional_paths:
        resolved_additional = _resolve_digest_input(additional, root)
        candidates.add(resolved_additional)
        if resolved_additional.is_file():
            continue
        elif resolved_additional.is_dir():
            for path in resolved_additional.rglob("*"):
                if _is_generated_digest_input(path, resolved_additional):
                    continue
                if path.is_symlink():
                    _resolve_digest_input(path, root)
                if path.is_file():
                    candidates.add(_resolve_digest_input(path, root))
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


def _validation_execution_inputs(root: Path) -> tuple[Path, ...]:
    """Return repository inputs that determine validation execution semantics."""
    return (
        root / ".mise.toml",
        root / "pyproject.toml",
        root / "uv.lock",
        root / "src" / "chart_manager" / "services" / "chart_config.py",
        root / "src" / "chart_manager" / "services" / "manifest_validation",
        root / "src" / "chart_manager" / "services" / "lifecycle",
        root / "src" / "chart_manager" / "integrations" / "helm.py",
        root / "src" / "chart_manager" / "integrations" / "kubeconform.py",
        root / "src" / "chart_manager" / "integrations" / "kyverno.py",
    )


def _resolve_digest_input(path: Path, root: Path) -> Path:
    """Resolve a digest input and reject symlinks escaping the repository."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise SpecError(f"digest input escapes repository root: {path} resolves to {resolved}")
    return resolved


def _is_generated_digest_input(path: Path, base: Path) -> bool:
    """Exclude generated Python cache files from execution-engine digests."""
    relative = path.relative_to(base)
    return "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}
