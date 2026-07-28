"""Pure changed-file impact analysis for validation and cluster-test CI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.services.cluster_test_catalog import ClusterTestCatalog
from chart_manager.services.lifecycle.models import LIFECYCLE_API_VERSION
from chart_manager.services.manifest_validation.planner import build_worklist
from chart_manager.settings import DEFAULT_CHARTS_DIR, RepositoryLayout


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

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-safe explanation."""
        return {
            "code": self.code.value,
            "changedFile": self.changed_file.as_posix(),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ValidationImpact:
    """One selected chart/environment validation case."""

    chart: str
    environment: str
    release: str
    namespace: str
    reasons: tuple[ImpactReason, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe validation selection."""
        return {
            "chart": self.chart,
            "environment": self.environment,
            "release": self.release,
            "namespace": self.namespace,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }


@dataclass(frozen=True)
class ClusterTestImpact:
    """One selected chart/profile live-cluster matrix entry."""

    chart: str
    profile: str
    reasons: tuple[ImpactReason, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe cluster-test matrix entry."""
        return {
            "chart": self.chart,
            "profile": self.profile,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }


@dataclass(frozen=True)
class LifecycleImpact:
    """Machine-readable lifecycle selection derived from explicit changes."""

    changed_files: tuple[Path, ...]
    validation: tuple[ValidationImpact, ...]
    cluster_tests: tuple[ClusterTestImpact, ...]
    spec_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the stable CI-facing projection."""
        return {
            "apiVersion": LIFECYCLE_API_VERSION,
            "kind": "LifecycleImpact",
            "changedFiles": [path.as_posix() for path in self.changed_files],
            "validationSelection": [case.to_dict() for case in self.validation],
            "clusterTestMatrix": [case.to_dict() for case in self.cluster_tests],
            "specErrors": list(self.spec_errors),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class _FanoutRule:
    """One repository-level cluster-test safety fanout rule."""

    name: str
    detail: str
    prefix: tuple[str, ...] | None = None
    exact: tuple[str, ...] | None = None

    def matches(self, path: Path) -> bool:
        parts = path.parts
        if self.exact is not None:
            return parts == self.exact
        assert self.prefix is not None
        return parts[: len(self.prefix)] == self.prefix


_STATIC_CLUSTER_FANOUT_RULES = (
    _FanoutRule(
        "chart-manager-code",
        "chart-manager implementation changes can affect every cluster workflow",
        prefix=("src", "chart_manager"),
    ),
    _FanoutRule(
        "kind-config",
        "the Kind cluster blueprint affects every ephemeral cluster",
        exact=("kind-config.yaml",),
    ),
    _FanoutRule(
        "mise-tool-pins",
        "tool version pins affect every cluster-test executor",
        exact=(".mise.toml",),
    ),
    _FanoutRule(
        "python-project",
        "Python dependency and command configuration affects cluster-test execution",
        exact=("pyproject.toml",),
    ),
    _FanoutRule(
        "python-lock",
        "locked Python dependencies affect cluster-test execution",
        exact=("uv.lock",),
    ),
    _FanoutRule(
        "ci-workflow",
        "the CI workflow controls every cluster-test matrix entry",
        exact=(".github", "workflows", "ci.yaml"),
    ),
)


class LifecycleImpactService:
    """Derive both lifecycle worklists from an explicit changed-file list."""

    def __init__(self, root: Path, *, charts_dir: Path = DEFAULT_CHARTS_DIR) -> None:
        self.layout = RepositoryLayout(root=root, charts_dir=charts_dir)
        self.root = self.layout.root
        self.cluster_catalog = ClusterTestCatalog(self.root, charts_dir=charts_dir)

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
            single = build_worklist(
                root=self.root,
                changed_files=[changed_file.as_posix()],
                charts_dir=self.layout.charts_dir,
            )
            for row in single.rows:
                key = (row.chart, row.env)
                _append_reason(
                    validation_reasons,
                    key,
                    _validation_reason(
                        changed_file,
                        selected_chart=row.chart,
                        layout=self.layout,
                    ),
                )

        combined = build_worklist(
            root=self.root,
            changed_files=[path.as_posix() for path in changes],
            charts_dir=self.layout.charts_dir,
        )
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
            (path, rule)
            for path in changes
            for rule in _cluster_fanout_rules(self.layout)
            if rule.matches(path)
        ]
        if fanout_matches:
            for chart in enabled:
                for path, rule in fanout_matches:
                    _append_reason(
                        selected,
                        (chart, profiles[chart]),
                        ImpactReason(
                            ImpactReasonCode.CLUSTER_SAFETY_FANOUT,
                            path,
                            f"{rule.name}: {rule.detail}",
                        ),
                    )

        enabled_set = set(enabled)
        for path in changes:
            changed_chart = self.layout.chart_name_from_repo_path(path)
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
                    target.spec.profile(reference.profile)
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


def analyze_lifecycle_impact(
    root: Path,
    changed_files: list[str] | tuple[str, ...],
    *,
    charts_dir: Path = DEFAULT_CHARTS_DIR,
) -> LifecycleImpact:
    """Convenience wrapper for explicit changed-file impact analysis."""
    return LifecycleImpactService(root, charts_dir=charts_dir).analyze(changed_files)


def _default_profile(profiles: Mapping[str, object]) -> str:
    """Preserve CI's minimal convention with a deterministic safe fallback."""
    if "minimal" in profiles:
        return "minimal"
    return sorted(profiles)[0]


def _validation_reason(
    changed_file: Path,
    *,
    selected_chart: str,
    layout: RepositoryLayout,
) -> ImpactReason:
    """Classify the existing validation worklist rule that selected a row."""
    parts = changed_file.parts
    if parts and parts[0] == "policies":
        return ImpactReason(
            ImpactReasonCode.REPOSITORY_POLICY,
            changed_file,
            "repository policy changes validate every configured environment",
        )
    if parts[:3] == ("src", "chart_manager", "services") or parts[:3] == (
        "src",
        "chart_manager",
        "integrations",
    ):
        return ImpactReason(
            ImpactReasonCode.VALIDATION_ENGINE,
            changed_file,
            "validation implementation changes validate every configured environment",
        )
    changed_chart = layout.chart_name_from_repo_path(changed_file)
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


def _cluster_fanout_rules(layout: RepositoryLayout) -> tuple[_FanoutRule, ...]:
    """Return static safety rules plus chart-root-relative shared prerequisites."""
    return (
        *_STATIC_CLUSTER_FANOUT_RULES,
        _FanoutRule(
            "cilium-bootstrap",
            "Cilium is environment-owned bootstrap used by every ephemeral cluster",
            prefix=(*layout.charts_dir.parts, "cilium"),
        ),
        _FanoutRule(
            "istio-base",
            "Istio base is a shared runtime prerequisite across cluster tests",
            prefix=(*layout.charts_dir.parts, "istio-base"),
        ),
    )


def _append_reason(
    sink: dict[tuple[str, str], list[ImpactReason]],
    key: tuple[str, str],
    reason: ImpactReason,
) -> None:
    """Append a reason once while retaining deterministic encounter order."""
    reasons = sink.setdefault(key, [])
    if reason not in reasons:
        reasons.append(reason)
