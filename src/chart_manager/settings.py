"""Process configuration and repository layout.

The managed Helm chart directory is repository layout, not chart-domain data.
It is loaded once at a composition boundary and passed to services so chart
discovery, changed-file classification, lifecycle planning, and upgrades all
agree on the same location.
"""

from __future__ import annotations

from pathlib import Path, PurePath
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CHARTS_DIR = Path("charts")
DEFAULT_LOCAL_CONFIG = Path(".chart-manager/local-cluster.yaml")
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["text", "json"]


def _validate_repository_dir(value: Path, *, field: str) -> Path:
    """Require a non-empty repository-relative path without traversal."""
    path = Path(value)
    if path.is_absolute():
        raise ValueError(f"{field} must be relative to the repository root")
    parts = path.parts
    if not parts or path == Path(".") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(
            f"{field} must be a non-empty repository-relative path without '.' or '..'"
        )
    return Path(*parts)


def validate_charts_dir(value: Path) -> Path:
    """Validate the managed chart directory."""
    return _validate_repository_dir(value, field="charts_dir")


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
    local_config: Path = DEFAULT_LOCAL_CONFIG
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "text"

    @field_validator("charts_dir")
    @classmethod
    def _validate_charts_directory(cls, value: Path) -> Path:
        return validate_charts_dir(value)

    @field_validator("local_config")
    @classmethod
    def _validate_local_config(cls, value: Path) -> Path:
        return _validate_repository_dir(value, field="local_config")

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("log_format", mode="before")
    @classmethod
    def _normalize_log_format(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

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
    "DEFAULT_LOCAL_CONFIG",
    "LogFormat",
    "LogLevel",
    "RepositoryLayout",
    "Settings",
    "validate_charts_dir",
]
