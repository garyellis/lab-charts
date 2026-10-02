"""Process configuration.

Repository layout is answered by `chart_manager.domain.workspace.
RepositoryWorkspace`, which `chart_manager.composition.Container` loads once
per root and hands to every service. This module deliberately imports nothing
from `domain/`: `domain/` sits below the composition root, and a `settings`
that reached into it while domain modules read their defaults from here was
an import cycle.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
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


#: Environment variables that once configured the repository layout. Layout
#: now lives only in `.chart-manager/workspace.yaml`; `extra="forbid"` rejects
#: these as config.yaml keys, but pydantic-settings ignores unknown
#: environment variables, so `Settings` checks for them itself rather than
#: letting a stale export silently stop applying. The marker path is spelled
#: out rather than imported from `domain.workspace`, which this module must
#: not import.
_REMOVED_ENV_VARS: dict[str, str] = {
    "CHART_MANAGER_CHARTS_DIR": "spec.chartsDir",
    "CHART_MANAGER_LOCAL_CONFIG": "spec.localCluster",
}


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
    #: Not validated as a repository-relative path: `.` is its default and an
    #: absolute path is the normal way to point at a checkout elsewhere.
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

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_layout_env(cls, data: object) -> object:
        """Fail on a removed layout variable instead of ignoring it.

        Raises `SpecError` rather than `ValueError` so it escapes pydantic
        unwrapped and reaches the operator as one `error:` line (exit 3),
        not as a validation traceback.
        """
        removed = [
            f"{name} was removed; set {field} in .chart-manager/workspace.yaml instead"
            for name, field in _REMOVED_ENV_VARS.items()
            if name in os.environ
        ]
        if removed:
            raise SpecError("; ".join(removed))
        return data

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
