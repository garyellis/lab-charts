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
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from chart_manager.domain.workspace import (
    LEGACY_CHARTS_DIR,
    LEGACY_LOCAL_CLUSTER,
    RepositoryWorkspace,
    load_repository_workspace,
    resolve_repository_root,
)
from chart_manager.plumbing.yaml_files import load_yaml_file

DEFAULT_CHARTS_DIR = LEGACY_CHARTS_DIR
DEFAULT_LOCAL_CONFIG = LEGACY_LOCAL_CLUSTER
DEFAULT_CONFIG_FILE = Path(".chart-manager/config.yaml")
DEFAULT_ROOT = Path(".")
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["text", "json"]

#: Where `Settings` looks for its optional YAML file.
#:
#: Module state rather than a constructor argument because pydantic-settings
#: resolves its sources from the *class*, not from per-instance kwargs --
#: there is no `Settings(config=...)` to thread through, and `Container`
#: builds its own default when no `Settings` is injected. The surface sets
#: this once from `--config` in `cli/main.py`'s root callback, before
#: anything constructs Settings; nothing else writes it.
_config_file: Path = DEFAULT_CONFIG_FILE


def config_file() -> Path:
    """Return the YAML config file `Settings` will read."""
    return _config_file


def set_config_file(path: Path) -> None:
    """Point `Settings` at a different YAML config file.

    Called once, from the CLI's root callback. An absent file is not an
    error: the file is optional and every field has a default.
    """
    global _config_file
    _config_file = path


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
    #: The repository this invocation operates on.
    #:
    #: Unlike `charts_dir` and `local_config` this is *not* validated as a
    #: repository-relative path: `.` is its default and an absolute path is
    #: the normal way to point at a checkout elsewhere.
    #:
    #: Explicit operator override. When absent, repository-bound entry points
    #: discover the nearest workspace marker; non-repository commands ignore it.
    root: Path = DEFAULT_ROOT

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order the sources as `CHART_MANAGER_* env > config.yaml > default`.

        Highest priority first. `init_settings` stays ahead of everything so
        an explicit `Settings(charts_dir=...)` in a test still wins.
        `dotenv_settings` is dropped: this project has no `.env` convention,
        and leaving it in would add a fourth, undocumented precedence step
        between env and the config file.

        Repository roots intentionally have no command-line source. Environment
        or this config file overrides nearest-marker discovery.
        """
        return (
            init_settings,
            env_settings,
            InitSettingsSource(
                settings_cls,
                load_yaml_file(config_file()) if config_file().is_file() else {},
            ),
            file_secret_settings,
        )

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

    def repository_workspace(
        self,
        root: Path | None = None,
        *,
        start: Path | None = None,
    ) -> RepositoryWorkspace:
        """Discover and compile one checkout-invariant repository policy."""
        configured_root = root
        if configured_root is None and "root" in self.model_fields_set:
            configured_root = self.root
        resolved_root = resolve_repository_root(configured=configured_root, start=start)
        explicit_legacy = bool(
            {"charts_dir", "local_config"}.intersection(self.model_fields_set)
        )
        return load_repository_workspace(
            resolved_root,
            legacy_charts_dir=self.charts_dir,
            legacy_local_cluster=self.local_config,
            legacy_layout_explicit=explicit_legacy,
        )


class RepositoryLayout:
    """Resolved repository root plus the configured managed-chart prefix."""

    def __init__(self, *, root: Path, charts_dir: Path = DEFAULT_CHARTS_DIR) -> None:
        self.root = root.resolve()
        self.charts_dir = (
            Path(".") if Path(charts_dir) == Path(".") else validate_charts_dir(charts_dir)
        )

    @property
    def charts_root(self) -> Path:
        """Absolute directory containing managed chart directories."""
        return self.root if self.charts_dir == Path(".") else self.root / self.charts_dir

    def chart_path(self, name: str) -> Path:
        """Absolute path for one managed chart name."""
        return self.charts_root / name

    def chart_name_from_repo_path(self, path: PurePath | str) -> str | None:
        """Return the managed chart name owning a repository-relative path."""
        parts = PurePath(path).parts
        prefix = () if self.charts_dir == Path(".") else self.charts_dir.parts
        if len(parts) <= len(prefix) or parts[: len(prefix)] != prefix:
            return None
        return parts[len(prefix)]

    def repo_chart_path(self, name: str, *children: str) -> Path:
        """Repository-relative path beneath one managed chart."""
        return self.charts_dir / name / Path(*children)


__all__ = [
    "DEFAULT_CHARTS_DIR",
    "DEFAULT_CONFIG_FILE",
    "DEFAULT_LOCAL_CONFIG",
    "DEFAULT_ROOT",
    "LogFormat",
    "LogLevel",
    "RepositoryLayout",
    "RepositoryWorkspace",
    "Settings",
    "config_file",
    "set_config_file",
    "validate_charts_dir",
]
