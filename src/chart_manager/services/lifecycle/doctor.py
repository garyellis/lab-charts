"""Repository-wide authored lifecycle configuration diagnostics."""

from __future__ import annotations

from pathlib import Path

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.chart_config import (
    LIFECYCLE_FILENAME,
    CapabilityStatus,
    ChartLifecycle,
    cluster_test_status,
    load_optional_chart_lifecycle,
    validate_chart_lifecycle_identity,
    validation_status,
)
from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.domain.charts import ChartRepository
from chart_manager.services.domain.cluster_tests import ClusterTestRef, ClusterTestSpec
from chart_manager.services.lifecycle.models import (
    DiagnosticSeverity,
    DoctorReport,
    LifecycleDiagnostic,
)
from chart_manager.services.manifest_validation.catalog import (
    load_manifest_validation_target,
)
from chart_manager.services.manifest_validation.compiler import (
    resolve_manifest_validation,
)
from chart_manager.settings import DEFAULT_CHARTS_DIR, RepositoryLayout


def doctor_lifecycle(
    root: Path, *, charts_dir: Path = DEFAULT_CHARTS_DIR
) -> DoctorReport:
    """Scan every chart config, runtime reference, input path, and requires cycle.

    This is deliberately observational: it parses and resolves authored data
    but does not invoke Helm, Kubernetes, or an environment bootstrap.
    """
    layout = RepositoryLayout(root=root, charts_dir=charts_dir)
    root = layout.root
    repository = ChartRepository(root, charts_dir=layout.charts_dir)
    chart_names = repository.list_names()
    diagnostics: list[LifecycleDiagnostic] = []
    lifecycles: dict[str, ChartLifecycle | None] = {}

    for name in chart_names:
        lifecycle_path = repository.charts_dir / name / LIFECYCLE_FILENAME
        try:
            chart = repository.get(name)
            lifecycle = load_optional_chart_lifecycle(lifecycle_path)
            lifecycles[name] = lifecycle
            if lifecycle is None:
                diagnostics.append(
                    _warning(
                        "missing-config",
                        f"chart has no {LIFECYCLE_FILENAME}; no lifecycle intent is authored",
                        chart=name,
                        path=lifecycle_path,
                    )
                )
            else:
                validate_chart_lifecycle_identity(
                    lifecycle,
                    chart_name=chart.name,
                    chart_directory=chart.path,
                )
        except ChartManagerError as exc:
            diagnostics.append(
                _error(
                    "invalid-config",
                    str(exc),
                    chart=name,
                    path=lifecycle_path,
                )
            )

    valid_names = set(lifecycles)
    cluster_specs: dict[str, ClusterTestSpec] = {}
    for name in sorted(valid_names):
        lifecycle = lifecycles[name]
        if (
            lifecycle is not None
            and cluster_test_status(lifecycle) is CapabilityStatus.ENABLED
        ):
            assert lifecycle.spec.cluster_test is not None
            cluster_specs[name] = lifecycle.spec.cluster_test

        if lifecycle is not None and validation_status(
            lifecycle
        ) is CapabilityStatus.ENABLED:
            try:
                target = load_manifest_validation_target(
                    root,
                    name,
                    charts_dir=layout.charts_dir,
                )
                resolve_manifest_validation(target, root)
            except ChartManagerError as exc:
                diagnostics.append(
                    _error(
                        "invalid-validation-input",
                        str(exc),
                        chart=name,
                        path=repository.charts_dir / name / LIFECYCLE_FILENAME,
                    )
                )

    catalog = ClusterTestCatalog(root, charts_dir=layout.charts_dir)
    for name, spec in sorted(cluster_specs.items()):
        for profile_name, profile in sorted(spec.profiles.items()):
            try:
                cluster_chart = catalog.get(name)
                catalog.value_paths(cluster_chart, profile_name)
            except ChartManagerError as exc:
                diagnostics.append(
                    _error(
                        "invalid-cluster-test-input",
                        str(exc),
                        chart=name,
                        profile=profile_name,
                        path=repository.charts_dir / name / LIFECYCLE_FILENAME,
                    )
                )
            for reference in profile.requires:
                diagnostic = _validate_ref(
                    reference,
                    source_chart=name,
                    source_profile=profile_name,
                    relation="requires",
                    all_chart_names=set(chart_names),
                    lifecycles=lifecycles,
                    cluster_specs=cluster_specs,
                    root=root,
                    charts_dir=layout.charts_dir,
                )
                if diagnostic is not None:
                    diagnostics.append(diagnostic)

        for reference in spec.dependent_tests:
            diagnostic = _validate_ref(
                reference,
                source_chart=name,
                source_profile=None,
                relation="dependentTests",
                all_chart_names=set(chart_names),
                lifecycles=lifecycles,
                cluster_specs=cluster_specs,
                root=root,
                charts_dir=layout.charts_dir,
            )
            if diagnostic is not None:
                diagnostics.append(diagnostic)

    diagnostics.extend(
        _cycle_diagnostics(
            cluster_specs,
            root,
            charts_dir=layout.charts_dir,
        )
    )
    diagnostics.sort(
        key=lambda item: (
            item.chart or "",
            item.profile or "",
            item.code,
            item.message,
        )
    )
    return DoctorReport(
        checked_charts=len(chart_names),
        diagnostics=tuple(diagnostics),
    )


def _validate_ref(
    reference: ClusterTestRef,
    *,
    source_chart: str,
    source_profile: str | None,
    relation: str,
    all_chart_names: set[str],
    lifecycles: dict[str, ChartLifecycle | None],
    cluster_specs: dict[str, ClusterTestSpec],
    root: Path,
    charts_dir: Path,
) -> LifecycleDiagnostic | None:
    """Validate one cross-chart runtime reference with a precise code."""
    label = f"{relation} reference {reference.chart}:{reference.profile}"
    source_path = root / charts_dir / source_chart / LIFECYCLE_FILENAME
    if reference.chart not in all_chart_names:
        return _error(
            "unknown-chart-reference",
            f"{label} names a chart that does not exist",
            chart=source_chart,
            profile=source_profile,
            path=source_path,
        )
    if reference.chart not in lifecycles:
        return _error(
            "invalid-chart-reference",
            f"{label} points to a chart with invalid configuration",
            chart=source_chart,
            profile=source_profile,
            path=source_path,
        )
    if reference.chart not in cluster_specs:
        return _error(
            "cluster-tests-unavailable",
            f"{label} points to a chart without enabled cluster tests",
            chart=source_chart,
            profile=source_profile,
            path=source_path,
        )
    target_spec = cluster_specs[reference.chart]
    if reference.profile not in target_spec.profiles:
        available = ", ".join(sorted(target_spec.profiles))
        return _error(
            "unknown-profile-reference",
            f"{label} names an unknown profile; available profiles: {available}",
            chart=source_chart,
            profile=source_profile,
            path=source_path,
        )
    return None


def _cycle_diagnostics(
    specs: dict[str, ClusterTestSpec],
    root: Path,
    *,
    charts_dir: Path,
) -> list[LifecycleDiagnostic]:
    """Find unique cycles in the authored runtime-requirement graph."""
    graph: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}
    for chart, spec in sorted(specs.items()):
        for profile_name, profile in sorted(spec.profiles.items()):
            graph[(chart, profile_name)] = tuple(
                (reference.chart, reference.profile)
                for reference in profile.requires
                if reference.chart in specs
                and reference.profile in specs[reference.chart].profiles
            )

    permanent: set[tuple[str, str]] = set()
    stack: list[tuple[str, str]] = []
    stack_set: set[tuple[str, str]] = set()
    seen_cycles: set[tuple[tuple[str, str], ...]] = set()
    diagnostics: list[LifecycleDiagnostic] = []

    def visit(node: tuple[str, str]) -> None:
        if node in permanent:
            return
        if node in stack_set:
            start = stack.index(node)
            cycle = (*stack[start:], node)
            canonical = _canonical_cycle(cycle)
            if canonical in seen_cycles:
                return
            seen_cycles.add(canonical)
            rendered = " -> ".join(f"{chart}:{profile}" for chart, profile in cycle)
            chart, profile = node
            diagnostics.append(
                _error(
                    "dependency-cycle",
                    f"cluster-test requirement cycle detected: {rendered}",
                    chart=chart,
                    profile=profile,
                    path=root / charts_dir / chart / LIFECYCLE_FILENAME,
                )
            )
            return
        stack.append(node)
        stack_set.add(node)
        for dependency in graph.get(node, ()):
            visit(dependency)
        stack.pop()
        stack_set.remove(node)
        permanent.add(node)

    for key in sorted(graph):
        visit(key)
    return diagnostics


def _canonical_cycle(
    cycle: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Canonicalize a closed directed cycle for duplicate suppression."""
    open_cycle = cycle[:-1]
    rotations = tuple(
        open_cycle[index:] + open_cycle[:index] for index in range(len(open_cycle))
    )
    return min(rotations)


def _error(
    code: str,
    message: str,
    *,
    chart: str | None = None,
    profile: str | None = None,
    path: Path | None = None,
) -> LifecycleDiagnostic:
    """Build an error finding without repeating boilerplate."""
    return LifecycleDiagnostic(
        severity=DiagnosticSeverity.ERROR,
        code=code,
        message=message,
        chart=chart,
        profile=profile,
        path=path,
    )


def _warning(
    code: str,
    message: str,
    *,
    chart: str | None = None,
    profile: str | None = None,
    path: Path | None = None,
) -> LifecycleDiagnostic:
    """Build a warning finding without repeating boilerplate."""
    return LifecycleDiagnostic(
        severity=DiagnosticSeverity.WARNING,
        code=code,
        message=message,
        chart=chart,
        profile=profile,
        path=path,
    )
