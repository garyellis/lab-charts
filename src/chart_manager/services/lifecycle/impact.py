"""Pure changed-file impact analysis for validation and cluster-test CI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from chart_manager.commands import test, validate
from chart_manager.shared.workspace import RepositoryWorkspace


class ImpactReasonCode(StrEnum):
    """Stable machine vocabulary explaining a selected validation case."""

    VALIDATION_TRIGGER = "validation-trigger"
    HELM_DEPENDENT = "helm-dependent"
    REPOSITORY_POLICY = "repository-policy"
    VALIDATION_ENGINE = "validation-engine"


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
class LifecycleImpact:
    """Machine-readable lifecycle selection derived from explicit changes."""

    changed_files: tuple[Path, ...]
    validation: tuple[ValidationImpact, ...]
    cluster_tests: tuple[test.SelectedTest, ...]
    spec_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class LifecycleImpactService:
    """Derive both lifecycle worklists from an explicit changed-file list."""

    def __init__(self, *, workspace: RepositoryWorkspace) -> None:
        self.workspace = workspace

    def analyze(self, changed_files: list[str] | tuple[str, ...]) -> LifecycleImpact:
        """Return deterministic validation selection and cluster-test matrix."""
        changes = tuple(
            sorted(
                {Path(raw) for raw in changed_files if raw},
                key=Path.as_posix,
            )
        )
        combined = validate.select([path.as_posix() for path in changes], workspace=self.workspace)
        validation = tuple(
            ValidationImpact(
                row.chart,
                row.env,
                row.release,
                row.namespace,
                tuple(
                    ImpactReason(ImpactReasonCode(reason.code), reason.changed_file, reason.detail)
                    for reason in combined.reasons[(row.chart, row.env)]
                ),
            )
            for row in combined.rows
        )

        tests = test.select([path.as_posix() for path in changes], workspace=self.workspace)
        return LifecycleImpact(
            changed_files=changes,
            validation=validation,
            cluster_tests=tests.tests,
            spec_errors=(*combined.spec_errors, *tests.spec_errors),
            warnings=combined.warnings,
        )


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


def _cluster_test_case(case: test.SelectedTest) -> dict[str, Any]:
    """JSON-serialize one selected chart/profile live-cluster matrix entry."""
    return {
        "chart": case.chart,
        "profile": case.profile,
        "reasons": [_reason(reason) for reason in case.reasons],
    }


def _reason(reason: ImpactReason | test.Reason) -> dict[str, Any]:
    """JSON-serialize one changed file and the rule that selected a case."""
    return {
        "code": reason.code.value,
        "changed_file": reason.changed_file.as_posix(),
        "detail": reason.detail,
    }
