"""Process configuration. Repository layout lives in `domain.workspace`."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.yaml_files import load_yaml_file

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


class Settings(BaseSettings):
    """Process-level adapter configuration. Repository layout is not here."""

    model_config = SettingsConfigDict(
        env_prefix="CHART_MANAGER_",
        frozen=True,
        extra="forbid",
    )

    kube_context: str | None = None
    docker_host: str | None = None
    command_timeout: float | None = None
    event_source: str = "chart-manager"
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "text"
    #: The repository this invocation operates on.
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
        an explicit `Settings(kube_context=...)` in a test still wins.
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

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("log_format", mode="before")
    @classmethod
    def _normalize_log_format(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value


def load_settings() -> Settings:
    """Build `Settings`, reporting invalid configuration as a `SpecError`."""
    try:
        return Settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors()
        )
        raise SpecError(f"invalid settings ({config_file()}): {problems}") from None


__all__ = [
    "DEFAULT_CONFIG_FILE",
    "DEFAULT_ROOT",
    "LogFormat",
    "LogLevel",
    "Settings",
    "config_file",
    "load_settings",
    "set_config_file",
]
