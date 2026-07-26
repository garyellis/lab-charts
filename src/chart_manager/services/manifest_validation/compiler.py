"""Compile authored manifest validation into cwd-independent runtime inputs."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from chart_manager.plumbing.errors import ChartNotFoundError, SpecError
from chart_manager.services.domain.charts import ChartRepository
from chart_manager.services.manifest_validation.models import (
    ManifestValidationTarget,
    WorklistRow,
)
from chart_manager.services.manifest_validation.runner import RowConfig
from chart_manager.services.manifest_validation.spec import resolve_namespace


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
    policy_paths: tuple[Path, ...]
    schema_locations: tuple[str, ...]
    warnings: tuple[str, ...] = ()


def discover_policies(repo_root: Path, chart_path: Path) -> list[Path]:
    """Return existing repository-wide and per-chart policy directories."""
    candidates = [repo_root / "policies", chart_path / "policies"]
    return [candidate.resolve() for candidate in candidates if candidate.is_dir()]


def resolve_chart_path(repo_root: Path, chart: str) -> tuple[Path, str]:
    """Resolve a repository chart name or an explicit fixture path."""
    if "/" in chart:
        candidate = Path(chart)
        if not candidate.is_absolute():
            candidate = (repo_root / candidate).resolve()
        if not (candidate / "Chart.yaml").is_file():
            raise ChartNotFoundError(f"no Chart.yaml at {candidate}")
        return candidate, candidate.name
    resolved = ChartRepository(repo_root).get(chart)
    return resolved.path, resolved.name


def resolve_values(chart_path: Path, values: Sequence[Path]) -> list[Path]:
    """Resolve explicit values or use the chart's default values file."""
    if values:
        return [
            value if value.is_absolute() else (chart_path / value).resolve() for value in values
        ]
    default = chart_path / "values.yaml"
    return [default.resolve()] if default.is_file() else []


def resolve_manifest_validation(
    target: ManifestValidationTarget,
    repo_root: Path,
) -> ResolvedManifestValidation:
    """Resolve an authored spec against its Helm chart and repository.

    ``policies.extra`` is chart-relative as its schema documents. For a
    migration window, an existing repository-relative directory is accepted
    only when the chart-relative path does not exist, and produces a warning.
    """
    root = repo_root.resolve()
    chart_path = target.path.resolve()
    environments: dict[str, ResolvedValidationEnvironment] = {}
    for name, authored_env in target.spec.environments.items():
        values = tuple(
            _compile_value_file(
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

    policies = discover_policies(root, chart_path)
    warnings: list[str] = []
    for extra in target.spec.policies.extra:
        chart_relative = (chart_path / extra).resolve()
        repo_relative = (root / extra).resolve()
        if chart_relative.is_dir():
            _require_within(
                chart_relative,
                chart_path,
                label=(f"{target.spec_path}: chart-relative policy directory {extra!r}"),
            )
            selected = chart_relative
        elif chart_relative.exists():
            warnings.append(f"{target.spec_path}: policy path is not a directory: {chart_relative}")
            continue
        elif repo_relative.is_dir():
            _require_within(
                repo_relative,
                root,
                label=(f"{target.spec_path}: repository-relative policy directory {extra!r}"),
            )
            selected = repo_relative
            warnings.append(
                f"{target.spec_path}: policy path {extra!r} is interpreted as "
                "repository-relative for compatibility; move it beneath the chart "
                "or update the authored path"
            )
        elif repo_relative.exists():
            warnings.append(f"{target.spec_path}: policy path is not a directory: {repo_relative}")
            continue
        else:
            # Extra policies were historically optional and silently omitted.
            # Keep that compatibility window non-fatal, but never omit one
            # without an actionable diagnostic.
            warnings.append(
                f"{target.spec_path}: policy directory does not exist: {chart_relative}"
            )
            continue
        if selected not in policies:
            policies.append(selected)

    schemas = tuple(
        _compile_schema_location(
            location,
            root,
            spec_path=target.spec_path,
        )
        for location in target.spec.schema_locations
    )
    return ResolvedManifestValidation(
        target=target,
        environments=environments,
        policy_paths=tuple(policies),
        schema_locations=schemas,
        warnings=tuple(warnings),
    )


def row_config_for(compiled: ResolvedManifestValidation, row: WorklistRow) -> RowConfig:
    """Build one runner configuration from already-compiled inputs."""
    try:
        environment = compiled.environments[row.env]
    except KeyError as exc:
        raise SpecError(
            f"unknown environment {row.env!r} for chart {compiled.target.name!r}"
        ) from exc
    return RowConfig(
        row=row,
        chart_path=compiled.target.path,
        values=list(environment.values),
        kubernetes_version=compiled.target.spec.kubernetes_version,
        schema_locations=list(compiled.schema_locations) or None,
        policy_paths=list(compiled.policy_paths),
    )


def _compile_value_file(
    value: str,
    *,
    chart_path: Path,
    environment: str,
    spec_path: Path,
) -> Path:
    """Resolve and validate one required chart-relative values file."""
    resolved = (chart_path / value).resolve()
    label = f"{spec_path}: environment {environment!r} value file {value!r}"
    _require_within(resolved, chart_path, label=label)
    if not resolved.exists():
        raise SpecError(f"{label} does not exist: {resolved}")
    if not resolved.is_file():
        raise SpecError(f"{label} is not a regular file: {resolved}")
    return resolved


def _compile_schema_location(
    location: str,
    repo_root: Path,
    *,
    spec_path: Path,
) -> str:
    """Keep kubeconform keywords/URLs and validate local schema templates."""
    if location == "default":
        return location
    parsed = urlsplit(location)
    if parsed.scheme:
        return location
    if not location.strip():
        raise SpecError(f"{spec_path}: schema location must not be empty")

    resolved = (repo_root / location).resolve()
    label = f"{spec_path}: local schema location {location!r}"
    _require_within(resolved, repo_root, label=label)

    template_start = location.find("{{")
    if template_start < 0:
        if not resolved.exists():
            raise SpecError(f"{label} does not exist: {resolved}")
        if not (resolved.is_file() or resolved.is_dir()):
            raise SpecError(f"{label} is not a regular file or directory: {resolved}")
        return str(resolved)

    static_prefix = location[:template_start]
    prefix_path = Path(static_prefix)
    anchor_relative = prefix_path if static_prefix.endswith(("/", "\\")) else prefix_path.parent
    anchor = (repo_root / anchor_relative).resolve()
    _require_within(anchor, repo_root, label=label)
    if not anchor.exists():
        raise SpecError(f"{label} has a missing template base directory: {anchor}")
    if not anchor.is_dir():
        raise SpecError(f"{label} template base is not a directory: {anchor}")
    return str(resolved)


def _require_within(path: Path, base: Path, *, label: str) -> None:
    """Reject resolved local inputs that escape their documented base."""
    if not path.is_relative_to(base):
        raise SpecError(f"{label} escapes its base directory: {path}")
