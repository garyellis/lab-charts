"""Safe path resolution shared by built-in validator providers."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from chart_manager.plumbing.errors import SpecError


def discover_policy_paths(repo_root: Path, chart_path: Path) -> tuple[Path, ...]:
    """Return existing repository-wide and per-chart policy directories."""
    return tuple(
        candidate.resolve()
        for candidate in (repo_root / "policies", chart_path / "policies")
        if candidate.is_dir()
    )


def resolve_policy_paths(
    *,
    repo_root: Path,
    chart_path: Path,
    spec_path: Path,
    extras: list[str],
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """Resolve discovered and authored chart-relative policy directories."""
    policies = list(discover_policy_paths(repo_root, chart_path))
    warnings: list[str] = []
    for extra in extras:
        selected = (chart_path / extra).resolve()
        if selected.is_dir():
            require_within(
                selected,
                chart_path,
                label=f"{spec_path}: chart-relative policy directory {extra!r}",
            )
        elif selected.exists():
            warnings.append(f"{spec_path}: policy path is not a directory: {selected}")
            continue
        else:
            warnings.append(f"{spec_path}: policy directory does not exist: {selected}")
            continue
        if selected not in policies:
            policies.append(selected)
    return tuple(policies), tuple(warnings)


def resolve_schema_locations(
    locations: list[str],
    *,
    repo_root: Path,
    spec_path: Path,
) -> tuple[str, ...]:
    """Keep kubeconform keywords/URLs and validate local schema templates."""
    return tuple(
        _resolve_schema_location(location, repo_root, spec_path=spec_path)
        for location in locations
    )


def _resolve_schema_location(
    location: str,
    repo_root: Path,
    *,
    spec_path: Path,
) -> str:
    if location == "default":
        return location
    parsed = urlsplit(location)
    if parsed.scheme:
        return location
    if not location.strip():
        raise SpecError(f"{spec_path}: schema location must not be empty")

    resolved = (repo_root / location).resolve()
    label = f"{spec_path}: local schema location {location!r}"
    require_within(resolved, repo_root, label=label)

    template_start = location.find("{{")
    if template_start < 0:
        if not resolved.exists():
            raise SpecError(f"{label} does not exist: {resolved}")
        if not (resolved.is_file() or resolved.is_dir()):
            raise SpecError(f"{label} is not a regular file or directory: {resolved}")
        return str(resolved)

    static_prefix = location[:template_start]
    prefix_path = Path(static_prefix)
    anchor_relative = (
        prefix_path if static_prefix.endswith(("/", "\\")) else prefix_path.parent
    )
    anchor = (repo_root / anchor_relative).resolve()
    require_within(anchor, repo_root, label=label)
    if not anchor.exists():
        raise SpecError(f"{label} has a missing template base directory: {anchor}")
    if not anchor.is_dir():
        raise SpecError(f"{label} template base is not a directory: {anchor}")
    return str(resolved)


def require_within(path: Path, base: Path, *, label: str) -> None:
    """Reject resolved local inputs that escape their documented base."""
    if not path.is_relative_to(base):
        raise SpecError(f"{label} escapes its base directory: {path}")
