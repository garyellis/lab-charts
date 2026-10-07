"""The vocabulary a preflight check speaks, and the binary probe adapters reuse.

Every integration owns its own preflight; `doctor` only aggregates the
results. A check carries an `Outcome`, never an exit code.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.exit_codes import Outcome

#: Wall-clock cap on one probe. Not `command_timeout`, which is unbounded by
#: default: a diagnostic must not hang on an unreachable daemon.
PROBE_TIMEOUT: Final = 5.0


class CheckStatus(StrEnum):
    """How one check came out.

    `SKIPPED` is not a failure, e.g. a kubecontext check when `kubectl` is absent.
    """

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Check:
    """One preflight result: what was checked, how it went, and what to do.

    Every failure carries a `remediation`.
    """

    name: str
    status: CheckStatus
    detail: str
    remediation: str | None = None
    #: What this failure means in `plumbing/exit_codes.py` terms. Always
    #: `SUCCESS` for a passing or skipped check.
    outcome: Outcome = Outcome.SUCCESS
    #: Optional structured evidence, such as counts and paths.
    data: Mapping[str, Any] | None = None

    @classmethod
    def ok(
        cls,
        name: str,
        detail: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> Check:
        """A check that passed."""
        return cls(name=name, status=CheckStatus.OK, detail=detail, data=data)

    @classmethod
    def skipped(
        cls,
        name: str,
        detail: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> Check:
        """A check that could not be answered, and is not itself a failure."""
        return cls(name=name, status=CheckStatus.SKIPPED, detail=detail, data=data)

    @classmethod
    def failed(
        cls,
        name: str,
        detail: str,
        *,
        remediation: str,
        outcome: Outcome,
        data: Mapping[str, Any] | None = None,
    ) -> Check:
        """A check that failed, with the fix and what it costs at the exit."""
        return cls(
            name=name,
            status=CheckStatus.FAILED,
            detail=detail,
            remediation=remediation,
            outcome=outcome,
            data=data,
        )

    def to_dict(self) -> dict[str, Any]:
        """The wire shape: name, status, detail, remediation.

        `data` is added only when present. `outcome` is absent: the report
        states it once, as `DoctorReport.outcome`.
        """
        result: dict[str, Any] = {
            "name": self.name,
            "status": str(self.status),
            "detail": self.detail,
            "remediation": self.remediation,
        }
        if self.data is not None:
            result["data"] = dict(self.data)
        return result


def first_line(text: str) -> str:
    """The first non-empty line of `text`, stripped; "" when there is none.

    The default version parser for `probe_binary`.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def probe_binary(
    runner: CommandRunner,
    binary: str,
    *,
    name: str,
    remediation: str,
    version_args: Sequence[str] = ("--version",),
    version_of: Callable[[str], str] = first_line,
    timeout: float = PROBE_TIMEOUT,
) -> Check:
    """Report whether `binary` is on PATH and, if so, what version it is.

      * not on PATH        -> `Outcome.MISSING_BINARY`, checked with
                              `shutil.which` so it costs no fork.
      * on PATH, ran badly -> `Outcome.TOOL`: installed but broken.
      * on PATH, ran fine  -> ok, with the version and the resolved path.

    `version_args=()` means "presence only", for tools with no version flag.
    """
    located = shutil.which(binary)
    if located is None:
        return Check.failed(
            name,
            f"{binary} not found on PATH",
            remediation=remediation,
            outcome=Outcome.MISSING_BINARY,
        )
    if not version_args:
        return Check.ok(name, located)
    try:
        result = runner.run([binary, *version_args], check=False, timeout=timeout)
    except ExternalCommandError as exc:
        # CommandTimeout, or MissingToolError if PATH changed since the lookup.
        return Check.failed(
            name, first_line(str(exc)), remediation=remediation, outcome=Outcome.TOOL
        )
    if result.returncode != 0:
        detail = first_line(result.stderr) or first_line(result.stdout)
        return Check.failed(
            name,
            detail or f"{binary} exited {result.returncode}",
            remediation=remediation,
            outcome=Outcome.TOOL,
        )
    version = version_of(result.stdout) or "version unknown"
    return Check.ok(name, f"{version} ({located})")


__all__ = [
    "PROBE_TIMEOUT",
    "Check",
    "CheckStatus",
    "first_line",
    "probe_binary",
]
