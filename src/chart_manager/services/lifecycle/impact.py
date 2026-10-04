"""Pure changed-file impact analysis for validation and cluster-test CI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from chart_manager.commands import validate
from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.shared.charts.chart_tests import ChartTestCatalog
from chart_manager.shared.charts.lifecycle import require_chart_test_profile
from chart_manager.shared.workspace import RepositoryWorkspace


class ImpactReasonCode(StrEnum):
    """Stable machine vocabulary explaining a selected lifecycle case."""

    CHART_CHANGE = "chart-change"
    VALIDATION_TRIGGER = "validation-trigger"
    HELM_DEPENDENT = "helm-dependent"
    REPOSITORY_POLICY = "repository-policy"
    VALIDATION_ENGINE = "validation-engine"
    DECLARED_DEPENDENT_TEST = "declared-dependent-test"
    CLUSTER_SAFETY_FANOUT = "cluster-safety-fanout"


@dataclass(frozen=True)
class ImpactReason:
    """One changed file and rule that selected a lifecycle case."""

    code: ImpactReasonCode
    changed_file: Path
    detail: str


@dataclass(frozen=True)
class ValidationImpact:
    """One selected chart/environment validation case."""

    chart: str
    environment: str
    release: str
    namespace: str
    reasons: tuple[ImpactReason, ...]


@dataclass(frozen=True)
class ClusterTestImpact:
    """One selected chart/profile live-cluster matrix entry."""

    chart: str
    profile: str
    reasons: tuple[ImpactReason, ...]


@dataclass(frozen=True)
class LifecycleImpact:
    """Machine-readable lifecycle selection derived from explicit changes."""

    changed_files: tuple[Path, ...]
    validation: tuple[ValidationImpact, ...]
    cluster_tests: tuple[ClusterTestImpact, ...]
    spec_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class LifecycleImpactService:
    """Derive both lifecycle worklists from an explicit changed-file list."""

    def __init__(self, *, workspace: RepositoryWorkspace) -> None:
        self.workspace = workspace
        self.root = workspace.root
        self.cluster_catalog = ChartTestCatalog(self.root, charts_dir=workspace.spec.charts_dir)

    def analyze(self, changed_files: list[str] | tuple[str, ...]) -> LifecycleImpact:
        """Return deterministic validation selection and cluster-test matrix."""
        changes = tuple(
            sorted(
                {Path(raw) for raw in changed_files if raw},
                key=Path.as_posix,
            )
        )
        validation_reasons: dict[tuple[str, str], list[ImpactReason]] = {}
        for changed_file in changes:
            single = validate.select([changed_file.as_posix()], workspace=self.workspace)
            for row in single.rows:
                key = (row.chart, row.env)
                _append_reason(
                    validation_reasons,
                    key,
                    _validation_reason(
                        changed_file,
                        selected_chart=row.chart,
                        workspace=self.workspace,
                    ),
                )

        combined = validate.select([path.as_posix() for path in changes], workspace=self.workspace)
        rows_by_key = {(row.chart, row.env): row for row in combined.rows}
        validation = tuple(
            ValidationImpact(
                chart,
                environment,
                rows_by_key[(chart, environment)].release,
                rows_by_key[(chart, environment)].namespace,
                tuple(reasons),
            )
            for (chart, environment), reasons in sorted(validation_reasons.items())
        )

        cluster_reasons, cluster_errors = self._cluster_test_impact(changes)
        cluster_tests = tuple(
            ClusterTestImpact(chart, profile, tuple(reasons))
            for (chart, profile), reasons in sorted(cluster_reasons.items())
        )
        return LifecycleImpact(
            changed_files=changes,
            validation=validation,
            cluster_tests=cluster_tests,
            spec_errors=(*combined.spec_errors, *cluster_errors),
            warnings=combined.warnings,
        )

    def default_cluster_test_profile(self, chart: str) -> str:
        """Resolve the shared CI default from one chart's authored profiles."""
        profiles = self.cluster_catalog.get(chart).spec.profiles
        if not profiles:
            raise SpecError(
                f"chart '{chart}' has enabled cluster tests but declares no profiles"
            )
        return _default_profile(profiles)

    def _cluster_test_impact(
        self,
        changes: tuple[Path, ...],
    ) -> tuple[dict[tuple[str, str], list[ImpactReason]], list[str]]:
        """Select cluster cases using typed safety and authored fanout rules."""
        enabled = self.cluster_catalog.enabled_names()
        profiles: dict[str, str] = {}
        for chart in enabled:
            profiles[chart] = self.default_cluster_test_profile(chart)

        selected: dict[tuple[str, str], list[ImpactReason]] = {}
        errors: list[str] = []
        fanout_matches = [
            (path, pattern)
            for path in changes
            for pattern in self.workspace.matching_cluster_test_patterns(path)
        ]
        if fanout_matches:
            for chart in enabled:
                for path, pattern in fanout_matches:
                    _append_reason(
                        selected,
                        (chart, profiles[chart]),
                        ImpactReason(
                            ImpactReasonCode.CLUSTER_SAFETY_FANOUT,
                            path,
                            self._cluster_fanout_detail(pattern),
                        ),
                    )

        enabled_set = set(enabled)
        for path in changes:
            changed_chart = self.workspace.chart_name_from_repo_path(path)
            if changed_chart is None:
                continue
            if changed_chart not in enabled_set:
                continue
            own_profile = profiles[changed_chart]
            _append_reason(
                selected,
                (changed_chart, own_profile),
                ImpactReason(
                    ImpactReasonCode.CHART_CHANGE,
                    path,
                    f"changed file belongs to enabled cluster-test chart {changed_chart}",
                ),
            )
            spec = self.cluster_catalog.get(changed_chart).spec
            for reference in spec.dependent_tests:
                try:
                    target = self.cluster_catalog.get(reference.chart)
                    require_chart_test_profile(target.spec, reference.profile)
                except ChartManagerError as exc:
                    errors.append(
                        f"{changed_chart} dependentTests "
                        f"{reference.chart}:{reference.profile}: {exc}"
                    )
                    continue
                _append_reason(
                    selected,
                    (reference.chart, reference.profile),
                    ImpactReason(
                        ImpactReasonCode.DECLARED_DEPENDENT_TEST,
                        path,
                        f"{changed_chart} declares dependent test "
                        f"{reference.chart}:{reference.profile}",
                    ),
                )
        return selected, errors

    def _cluster_fanout_detail(self, pattern: str) -> str:
        for chart in self.workspace.spec.cluster_test.shared_prerequisites:
            if pattern == self.workspace.repo_chart_path(chart).as_posix():
                return f"{chart} is a shared runtime prerequisite across cluster tests"
        if pattern not in self.workspace.spec.fanout.cluster_test and pattern not in {
            self.workspace.spec.local_cluster.as_posix(),
            self.workspace.marker.relative_to(self.workspace.root).as_posix(),
        }:
            return f"{pattern} is a LocalCluster bootstrap prerequisite used by every cluster test"
        return f"workspace cluster-test fanout matched {pattern}"


def _default_profile(profiles: Mapping[str, object]) -> str:
    """Preserve CI's minimal convention with a deterministic safe fallback."""
    if "minimal" in profiles:
        return "minimal"
    return sorted(profiles)[0]


def _validation_reason(
    changed_file: Path,
    *,
    selected_chart: str,
    workspace: RepositoryWorkspace,
) -> ImpactReason:
    """Classify the existing validation worklist rule that selected a row."""
    if changed_file == workspace.marker.relative_to(workspace.root) or _path_is_within(
        changed_file, workspace.spec.policies_dir
    ):
        return ImpactReason(
            ImpactReasonCode.REPOSITORY_POLICY,
            changed_file,
            "repository policy changes validate every configured environment",
        )
    if workspace.matches_validation_fanout(changed_file):
        return ImpactReason(
            ImpactReasonCode.VALIDATION_ENGINE,
            changed_file,
            "validation implementation changes validate every configured environment",
        )
    changed_chart = workspace.chart_name_from_repo_path(changed_file)
    if changed_chart is not None and changed_chart != selected_chart:
        return ImpactReason(
            ImpactReasonCode.HELM_DEPENDENT,
            changed_file,
            f"{selected_chart} declares a Helm dependency on {changed_chart}",
        )
    return ImpactReason(
        ImpactReasonCode.VALIDATION_TRIGGER,
        changed_file,
        f"authored validation triggers selected {selected_chart}",
    )


def _path_is_within(path: Path, relative: Path) -> bool:
    prefix = relative.parts
    return path.parts[: len(prefix)] == prefix


def _append_reason(
    sink: dict[tuple[str, str], list[ImpactReason]],
    key: tuple[str, str],
    reason: ImpactReason,
) -> None:
    """Append a reason once while retaining deterministic encounter order."""
    reasons = sink.setdefault(key, [])
    if reason not in reasons:
        reasons.append(reason)


def impact_to_dict(impact: LifecycleImpact) -> dict[str, Any]:
    """Project a `LifecycleImpact` onto the wire payload.

    `spec_errors` is carried in the document rather than replacing it: the
    selection derived from the files that *did* parse is still the answer to
    the question asked, and the CLI exits non-zero off the same list -- see
    `cli/plan.py` for why that is a spec exit rather than a generic failure.
    """
    return {
        "changed_files": [path.as_posix() for path in impact.changed_files],
        "validation_selection": [_validation_case(case) for case in impact.validation],
        "cluster_test_matrix": [_cluster_test_case(case) for case in impact.cluster_tests],
        "spec_errors": list(impact.spec_errors),
        "warnings": list(impact.warnings),
    }


def _validation_case(case: ValidationImpact) -> dict[str, Any]:
    """JSON-serialize one selected chart/environment validation case."""
    return {
        "chart": case.chart,
        "environment": case.environment,
        "release": case.release,
        "namespace": case.namespace,
        "reasons": [_reason(reason) for reason in case.reasons],
    }


def _cluster_test_case(case: ClusterTestImpact) -> dict[str, Any]:
    """JSON-serialize one selected chart/profile live-cluster matrix entry."""
    return {
        "chart": case.chart,
        "profile": case.profile,
        "reasons": [_reason(reason) for reason in case.reasons],
    }


def _reason(reason: ImpactReason) -> dict[str, Any]:
    """JSON-serialize one changed file and the rule that selected a case."""
    return {
        "code": reason.code.value,
        "changed_file": reason.changed_file.as_posix(),
        "detail": reason.detail,
    }
