"""Renovate CLI adapter for scoped chart dependency upgrades.

Renovate has three distinct configuration layers in this integration:

* ``RENOVATE_CONFIG_FILE`` points at self-hosted (global) policy;
* ``RENOVATE_ADDITIONAL_CONFIG_FILE`` optionally adds generated chart policy;
* ``RENOVATE_CONFIG`` carries the request-scoped runtime overlay.

The repository's normal ``renovate.json`` is intentionally not passed through
either file variable. Renovate discovers it as repository config after cloning,
so it is loaded exactly once.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from chart_manager.plumbing.commands import CommandRunner, SubprocessRunner
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import Check, probe_binary

DryRunMode = Literal["extract", "lookup", "full"]

_EMPTY_CONFIG: Mapping[str, object] = MappingProxyType({})
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$")
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenovateRequest:
    """One self-hosted Renovate invocation.

    ``token`` is a dedicated field so callers never need to put a credential
    in argv or in the JSON overlay. The adapter supplies it only as
    ``RENOVATE_TOKEN`` in the child environment.
    """

    repo_root: Path
    repository: str
    global_config_path: Path
    additional_config_path: Path | None = None
    runtime_overlay: Mapping[str, object] = field(default_factory=lambda: _EMPTY_CONFIG)
    dry_run: DryRunMode | None = None
    token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class RenovateResult:
    """Captured result of a Renovate run, including non-zero tool exits."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether Renovate exited successfully."""
        return self.returncode == 0


class Renovate:
    """Run self-hosted Renovate through the shared subprocess seam."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        binary: str | Path | None = None,
        validator_binary: str | Path | None = None,
        timeout: float | None = None,
    ) -> None:
        """Bind the runner, CLI paths, and optional wall-clock timeout."""
        self.runner = runner or SubprocessRunner()
        self._binary = str(binary) if binary is not None else "renovate"
        self._validator_binary = (
            str(validator_binary) if validator_binary is not None else "renovate-config-validator"
        )
        self.timeout = timeout

    def preflight(self) -> tuple[Check, ...]:
        """Report both Renovate binaries and whether a token is configured.

        The validator is checked for *presence only*: it is a node bin stub
        with no version flag, so asking for one would report a working
        install as broken.

        The token check is here rather than in the composition root that
        reads the variable, because "what does Renovate need to be able to
        run" is this adapter's knowledge. It is the case MY_COMMENTS.md
        names explicitly -- required environment is a per-integration
        preflight matter, not a surface one.
        """
        return (
            probe_binary(
                self.runner,
                self._binary,
                name="renovate",
                remediation="install Renovate -- `npm install -g renovate`",
            ),
            probe_binary(
                self.runner,
                self._validator_binary,
                name="renovate-config-validator",
                version_args=(),
                remediation="ships with Renovate -- `npm install -g renovate`",
            ),
            _token_check(),
        )

    def run(self, request: RenovateRequest) -> RenovateResult:
        """Run Renovate for exactly one repository.

        Non-zero Renovate exits are returned to the service layer, which owns
        the user-facing outcome. Local request/config errors are raised as
        ``ChartManagerError`` so they follow the existing expected-error path.
        """
        repo_root = _require_directory(request.repo_root, label="repository root")
        global_config = _require_file(
            request.global_config_path,
            relative_to=repo_root,
            label="Renovate global config",
        )
        additional_config = None
        if request.additional_config_path is not None:
            additional_config = _require_file(
                request.additional_config_path,
                relative_to=repo_root,
                label="Renovate additional config",
            )
        _validate_repository(request.repository)
        overlay = _serialize_overlay(request.runtime_overlay)

        env = {
            "RENOVATE_CONFIG_FILE": str(global_config),
            "RENOVATE_CONFIG": overlay,
        }
        if additional_config is not None:
            env["RENOVATE_ADDITIONAL_CONFIG_FILE"] = str(additional_config)
        if request.dry_run is not None:
            env["RENOVATE_DRY_RUN"] = request.dry_run
        if request.token is not None:
            env["RENOVATE_TOKEN"] = request.token

        mode = f"dry-run={request.dry_run}" if request.dry_run is not None else "write"
        _LOG.info("Starting Renovate for %s (%s)", request.repository, mode)
        _LOG.debug("Repository root: %s", repo_root)
        _LOG.debug("Global config: %s", global_config)
        if additional_config is not None:
            _LOG.debug("Additional config: %s", additional_config)
        _LOG.debug(
            "Authentication: %s",
            "configured" if request.token is not None else "not configured",
        )
        result = self.runner.run(
            [self._binary, request.repository],
            cwd=repo_root,
            check=False,
            timeout=self.timeout,
            env=env,
        )
        stdout = _redact_token(result.stdout, request.token)
        stderr = _redact_token(result.stderr, request.token)
        _log_subprocess_output(stdout, error=False)
        _log_subprocess_output(stderr, error=True)
        if result.returncode == 0:
            _LOG.info("Renovate completed for %s", request.repository)
        else:
            _LOG.error(
                "Renovate failed for %s (exit %s)",
                request.repository,
                result.returncode,
            )
        return RenovateResult(
            returncode=result.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def validate_config(
        self,
        paths: Sequence[Path],
        *,
        repo_root: Path,
        global_config: bool,
        strict: bool = True,
    ) -> RenovateResult:
        """Validate explicit config files with Renovate's bundled validator.

        Explicit paths are interpreted as self-hosted config by the validator.
        ``global_config=False`` adds its documented ``--no-global`` switch for
        repository config such as root ``renovate.json``.
        """
        root = _require_directory(repo_root, label="repository root")
        if not paths:
            raise ChartManagerError("Renovate validation needs at least one config path")
        resolved = [
            _require_file(path, relative_to=root, label="Renovate config") for path in paths
        ]
        args = [self._validator_binary]
        if strict:
            args.append("--strict")
        if not global_config:
            args.append("--no-global")
        args.extend(str(path) for path in resolved)
        result = self.runner.run(args, cwd=root, timeout=self.timeout)
        return RenovateResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def _require_directory(path: Path, *, label: str) -> Path:
    candidate = path.expanduser()
    if not candidate.is_dir():
        raise ChartManagerError(f"{label} is not a directory: {path}")
    return candidate.resolve()


def _require_file(path: Path, *, relative_to: Path, label: str) -> Path:
    candidate = path if path.is_absolute() else relative_to / path
    if not candidate.is_file():
        raise ChartManagerError(f"{label} is not a file: {path}")
    return candidate.resolve()


def _validate_repository(repository: str) -> None:
    # Apart from making mistakes obvious, requiring a slug prevents a value
    # beginning with "--" from being interpreted as another Renovate option.
    if not _REPOSITORY_RE.fullmatch(repository):
        raise ChartManagerError(
            "Renovate repository must be a slash-separated repository slug "
            f"(for example 'owner/repo'), got: {repository!r}"
        )


def _serialize_overlay(overlay: Mapping[str, object]) -> str:
    try:
        return json.dumps(dict(overlay), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ChartManagerError(
            f"Renovate runtime overlay is not JSON serializable: {exc}"
        ) from exc


def _redact_token(value: str, token: str | None) -> str:
    """Defensively mask a token if a failing child happens to echo its env."""
    if not token:
        return value
    return value.replace(token, "***")


def _log_subprocess_output(value: str, *, error: bool) -> None:
    """Forward captured Renovate output through the configured process logger."""
    for line in value.rstrip().splitlines():
        stripped = line.lstrip()
        if error or stripped.startswith(("ERROR:", "FATAL:")):
            _LOG.error("renovate> %s", line)
        elif stripped.startswith("WARN:"):
            _LOG.warning("renovate> %s", line)
        elif stripped.startswith("DEBUG:"):
            _LOG.debug("renovate> %s", line)
        else:
            _LOG.info("renovate> %s", line)


def _token_check() -> Check:
    """Whether a credential Renovate can authenticate with is in the environment.

    The same two names, in the same order, that `composition.Container`
    hands to `RenovateRequest.token`: Renovate spells its own setting
    RENOVATE_TOKEN, while GitHub Actions exposes its repository token as
    GITHUB_TOKEN. Reported as ENVIRONMENT rather than SPEC -- nothing the
    author wrote is wrong, the process just was not given a credential.
    """
    for variable in ("RENOVATE_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(variable):
            return Check.ok("renovate-token", f"{variable} is set")
    return Check.failed(
        "renovate-token",
        "neither RENOVATE_TOKEN nor GITHUB_TOKEN is set",
        remediation="export RENOVATE_TOKEN with a token that can read and open PRs",
        outcome=Outcome.ENVIRONMENT,
    )
