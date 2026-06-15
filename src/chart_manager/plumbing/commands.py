from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.errors import ExternalCommandError


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                list(args),
                cwd=cwd,
                check=False,
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # Always surface as ExternalCommandError so callers (phases, runner)
            # route timeouts through the same exit-2/tool-error code path as
            # any other subprocess failure. Partial output on the exc is
            # bytes-or-str depending on text mode; coerce safely.
            command = " ".join(args)
            partial = _decode(exc.stderr) or _decode(exc.stdout)
            detail = f"timed out after {timeout}s"
            if partial:
                detail += f"\n{partial.strip()}"
            raise ExternalCommandError(f"command timed out: {command}\n{detail}") from exc
        result = CommandResult(
            args=tuple(args),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        if check and result.returncode != 0:
            command = " ".join(result.args)
            detail = result.stderr.strip() or result.stdout.strip()
            raise ExternalCommandError(f"command failed ({result.returncode}): {command}\n{detail}")
        return result


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
