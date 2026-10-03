"""Process configuration.

Repository layout is answered by `chart_manager.domain.workspace.
RepositoryWorkspace`, which `chart_manager.composition.Container` loads once
per root and hands to every service. This module deliberately imports nothing
from `domain/`: `domain/` sits below the composition root, and a `settings`
that reached into it while domain modules read their defaults from here was
an import cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from chart_manager.plumbing.yaml_files import load_yaml_file

#: Defaults for the legacy layout fields, used only when a repository has no
#: `.chart-manager/workspace.yaml`. Spelled out here rather than imported from
#: `domain.workspace` (whose `LEGACY_*` constants carry the same values) so this
#: module stays free of `domain/`; `tests/test_workspace.py` pins the two
#: spellings together. Both go when the legacy fallback does.
_LEGACY_CHARTS_DIR = Path("charts")
_LEGACY_LOCAL_CONFIG = Path(".chart-manager/local-cluster.yaml")
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


class Settings(BaseSettings):
    """Process-level adapter configuration plus the legacy layout fallback."""

    model_config = SettingsConfigDict(
        env_prefix="CHART_MANAGER_",
        frozen=True,
        extra="ignore",
    )

    kube_context: str | None = None
    docker_host: str | None = None
    command_timeout: float | None = None
    event_source: str = "chart-manager"
    #: Legacy layout, read only when the repository has no workspace.yaml.
    charts_dir: Path = _LEGACY_CHARTS_DIR
    local_config: Path = _LEGACY_LOCAL_CONFIG
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
        return _validate_repository_dir(value, field="charts_dir")

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


__all__ = [
    "DEFAULT_CONFIG_FILE",
    "DEFAULT_ROOT",
    "LogFormat",
    "LogLevel",
    "Settings",
    "config_file",
    "set_config_file",
]
