"""Thin subprocess wrapper that normalizes failures into ExternalCommandError."""
from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from chart_manager.plumbing.errors import (
    CommandTimeout,
    ExternalCommandError,
    MissingToolError,
)


@dataclass(frozen=True)
class CommandResult:
    """Captured outcome of a completed subprocess."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """The subprocess seam every adapter is built against.

    A Protocol rather than a base class, deliberately. This name used to be
    the concrete implementation and eight test files subclassed it, each
    hand-copying the `run` signature and never calling `super().__init__`.
    Adding a single keyword therefore broke every fake at once, so the seam
    could not evolve -- which is the documented reason `env` was missing for
    so long, and in turn why `Kubectl` and `Kind` had nowhere to put a
    cluster address and read the ambient one instead. Structural typing means
    a fake satisfies the seam by shape, and a keyword can be added here in
    one edit.

    `SubprocessRunner` is the production implementation;
    `tests/conftest.py:FakeCommandRunner` is the only fake.
    """

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run `args` and return its outcome.

        `env` is *overlaid* on the parent environment, not substituted for
        it, so a caller can scope one variable (KUBECONFIG, DOCKER_HOST)
        to a single invocation without rebuilding PATH.

        Raises ExternalCommandError on non-zero exit (when `check`) or
        timeout; never raises CalledProcessError/TimeoutExpired directly.
        """
        ...


class SubprocessRunner:
    """The production `CommandRunner`: runs argv through `subprocess.run`."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run `args`, returning a CommandResult. See `CommandRunner.run`."""
        # Overlay, never substitute. A bare `env=` drops PATH, and the next
        # thing that happens is that the tool is not found -- which surfaces
        # as MissingToolError and reads like a broken install rather than
        # like the caller having scoped one variable.
        child_env = {**os.environ, **env} if env is not None else None
        try:
            completed = subprocess.run(
                list(args),
                cwd=cwd,
                check=False,
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                timeout=timeout,
                env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            # Always surface as ExternalCommandError so callers (phases, runner)
            # route timeouts through the same exit-2/tool-error code path as
            # any other subprocess failure. Partial output on the exc is
            # bytes-or-str depending on text mode; coerce safely.
            command = redact(args)
            partial = _decode(exc.stderr) or _decode(exc.stdout)
            detail = f"timed out after {timeout}s"
            if partial:
                detail += f"\n{partial.strip()}"
            raise CommandTimeout(
                f"command timed out: {command}\n{detail}",
                stderr=partial,
            ) from exc
        except FileNotFoundError as exc:
            # subprocess raises this when the executable itself is absent.
            # Left bare it escapes the ChartManagerError hierarchy entirely,
            # so best-effort handlers that degrade on a *failing* tool would
            # instead crash on a *missing* one.
            raise MissingToolError(f"required tool not found on PATH: {args[0]}") from exc
        result = CommandResult(
            args=tuple(args),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        if check and result.returncode != 0:
            command = redact(result.args)
            detail = result.stderr.strip() or result.stdout.strip()
            raise ExternalCommandError(
                f"command failed ({result.returncode}): {command}\n{detail}",
                stderr=result.stderr,
                returncode=result.returncode,
            )
        return result


#: Flags whose *value* is a credential. `--set` is included because helm
#: renders `--set key=value` pairs straight into argv, and a rendered error
#: message travels into logs and event payloads.
_SECRET_FLAGS = frozenset(
    {"--set", "--set-string", "--token", "--password", "--docker-password"}
)
_MASK = "***"


def redact(args: Sequence[str]) -> str:
    """Render argv for display with credential values masked.

    Keeps the key half of `--set key=value` so the message still says which
    setting failed, and masks only the value.
    """
    rendered: list[str] = []
    mask_next = False
    for arg in args:
        if mask_next:
            key, sep, _ = arg.partition("=")
            rendered.append(f"{key}={_MASK}" if sep else _MASK)
            mask_next = False
            continue
        flag, sep, _ = arg.partition("=")
        if flag in _SECRET_FLAGS:
            if sep:
                rendered.append(f"{flag}={_MASK}")
            else:
                rendered.append(arg)
                mask_next = True
            continue
        rendered.append(arg)
    return " ".join(rendered)


def _decode(value: object) -> str:
    """Coerce bytes/str/None subprocess output to str."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
