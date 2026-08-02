"""Resolve authored manifest validation into cwd-independent runtime inputs.

Named for what it does. It compiles nothing in the `lifecycle/compiler.py`
sense -- there is no plan, no action list, no execution graph. It takes an
authored spec plus its chart on disk and answers "which absolute paths, which
namespace, which validator configs", which is resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.errors import SpecError
from chart_manager.services.manifest_validation.models import (
    ManifestValidationTarget,
    WorklistRow,
)
from chart_manager.services.manifest_validation.namespaces import resolve_namespace
from chart_manager.services.manifest_validation.paths import require_within
from chart_manager.services.manifest_validation.runner import RowConfig
from chart_manager.services.manifest_validation.validator_adapters import (
    VALIDATOR_REGISTRY,
)
from chart_manager.services.manifest_validation.validators import (
    ValidatorCompileContext,
    ValidatorInvocation,
    ValidatorProvider,
)


@dataclass(frozen=True)
class ResolvedValidationEnvironment:
    """Resolved runtime inputs for one authored validation environment."""

    name: str
    namespace: str
    values: tuple[Path, ...]


@dataclass(frozen=True)
class ResolvedManifestValidation:
    """Cwd-independent runtime configuration for manifest validation."""

    target: ManifestValidationTarget
    environments: dict[str, ResolvedValidationEnvironment]
    validator_invocations: tuple[ValidatorInvocation, ...] = ()
    warnings: tuple[str, ...] = ()


def resolve_manifest_validation(
    target: ManifestValidationTarget,
    repo_root: Path,
    *,
    providers: tuple[ValidatorProvider, ...] = VALIDATOR_REGISTRY,
) -> ResolvedManifestValidation:
    """Resolve an authored spec against its Helm chart and repository."""
    root = repo_root.resolve()
    chart_path = target.path.resolve()
    environments: dict[str, ResolvedValidationEnvironment] = {}
    for name, authored_env in target.spec.environments.items():
        values = tuple(
            _resolve_value_file(
                value,
                chart_path=chart_path,
                environment=name,
                spec_path=target.spec_path,
            )
            for value in authored_env.values
        )
        environments[name] = ResolvedValidationEnvironment(
            name=name,
            namespace=resolve_namespace(target.spec, name),
            values=values,
        )

    context = ValidatorCompileContext(
        spec=target.spec,
        repo_root=root,
        chart_path=chart_path,
        spec_path=target.spec_path,
    )
    invocations = tuple(
        provider.compile(context)
        for provider in providers
    )
    return ResolvedManifestValidation(
        target=target,
        environments=environments,
        validator_invocations=invocations,
        warnings=tuple(
            warning
            for invocation in invocations
            for warning in invocation.warnings
        ),
    )


def row_config_for(resolved: ResolvedManifestValidation, row: WorklistRow) -> RowConfig:
    """Build one runner configuration from already-resolved inputs."""
    try:
        environment = resolved.environments[row.env]
    except KeyError as exc:
        raise SpecError(
            f"unknown environment {row.env!r} for chart {resolved.target.name!r}"
        ) from exc
    return RowConfig(
        row=row,
        chart_path=resolved.target.path,
        values=list(environment.values),
        validator_invocations=resolved.validator_invocations,
    )


def _resolve_value_file(
    value: str,
    *,
    chart_path: Path,
    environment: str,
    spec_path: Path,
) -> Path:
    """Resolve and validate one required chart-relative values file."""
    resolved = (chart_path / value).resolve()
    label = f"{spec_path}: environment {environment!r} value file {value!r}"
    require_within(resolved, chart_path, label=label)
    if not resolved.exists():
        raise SpecError(f"{label} does not exist: {resolved}")
    if not resolved.is_file():
        raise SpecError(f"{label} is not a regular file: {resolved}")
    return resolved
