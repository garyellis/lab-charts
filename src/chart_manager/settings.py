"""Process configuration and repository layout.

The managed Helm chart directory is repository layout, not chart-domain data.
It is loaded once at a composition boundary and passed to services so chart
discovery, changed-file classification, lifecycle planning, and upgrades all
agree on the same location.
"""

from __future__ import annotations

from pathlib import Path, PurePath

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CHARTS_DIR = Path("charts")


def validate_charts_dir(value: Path) -> Path:
    """Require a non-empty repository-relative path without traversal."""
    path = Path(value)
    if path.is_absolute():
        raise ValueError("charts_dir must be relative to the repository root")
    parts = path.parts
    if not parts or path == Path(".") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(
            "charts_dir must be a non-empty repository-relative path without '.' or '..'"
        )
    return Path(*parts)


class Settings(BaseSettings):
    """Process-level adapter and repository-layout configuration."""

    model_config = SettingsConfigDict(
        env_prefix="CHART_MANAGER_",
        frozen=True,
        extra="ignore",
    )

    kube_context: str | None = None
    docker_host: str | None = None
    command_timeout: float | None = None
    event_source: str = "chart-manager"
    charts_dir: Path = DEFAULT_CHARTS_DIR

    @field_validator("charts_dir")
    @classmethod
    def _validate_charts_dir(cls, value: Path) -> Path:
        return validate_charts_dir(value)

    def layout(self, root: Path) -> RepositoryLayout:
        """Bind this process configuration to one repository root."""
        return RepositoryLayout(root=root, charts_dir=self.charts_dir)


class RepositoryLayout:
    """Resolved repository root plus the configured managed-chart prefix."""

    def __init__(self, *, root: Path, charts_dir: Path = DEFAULT_CHARTS_DIR) -> None:
        self.root = root.resolve()
        self.charts_dir = validate_charts_dir(charts_dir)

    @property
    def charts_root(self) -> Path:
        """Absolute directory containing managed chart directories."""
        return self.root / self.charts_dir

    def chart_path(self, name: str) -> Path:
        """Absolute path for one managed chart name."""
        return self.charts_root / name

    def chart_name_from_repo_path(self, path: PurePath | str) -> str | None:
        """Return the managed chart name owning a repository-relative path."""
        parts = PurePath(path).parts
        prefix = self.charts_dir.parts
        if len(parts) <= len(prefix) or parts[: len(prefix)] != prefix:
            return None
        return parts[len(prefix)]

    def repo_chart_path(self, name: str, *children: str) -> Path:
        """Repository-relative path beneath one managed chart."""
        return self.charts_dir / name / Path(*children)


__all__ = [
    "DEFAULT_CHARTS_DIR",
    "RepositoryLayout",
    "Settings",
    "validate_charts_dir",
]
