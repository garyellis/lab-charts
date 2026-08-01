"""The vocabulary a preflight check speaks, and the binary probe adapters reuse.

Ownership, stated once so it is never re-derived:

    Every integration owns its own preflight. If an adapter shells out to a
    binary, the check for that binary lives beside the adapter; if it needs
    an environment variable or a kubeconfig entry, that check lives there
    too. `doctor` is a *surface* that aggregates the results -- it does not
    know what `kubeconform` is called or which flag prints its version.

That position is recorded in `MY_COMMENTS.md` and in the design doc's P0
bullet, and it is why this module holds only the shared *shape* of a result
plus the one probe every adapter would otherwise hand-roll. Nothing here
knows about any specific tool.

Why the result carries an `Outcome` rather than an exit code
------------------------------------------------------------
`Check.outcome` is the semantic vocabulary from `plumbing/exit_codes.py`, so
an adapter states "this is a missing binary" or "this is an environment
problem" and never "this is 127". The number is `cli/doctor.py`'s call,
looked up through `exit_code_for` exactly like `cli/helmrelease.py` looks up
a promote outcome. An adapter that wrote an integer here would be the second
place in the codebase that decides what a failure is worth, which is the
thing `exit_codes.py` exists to prevent.

Why `shutil.which` and not `runner.run` alone
----------------------------------------------
"Is it on PATH" and "does it run" are different failures with different exit
codes (127 vs 4) and different remediations ("install it" vs "it is broken").
`SubprocessRunner` does raise `MissingToolError` for an absent executable,
but only after paying for a fork, and a `CommandRunner` fake is under no
obligation to reproduce that. Resolving the path first makes the missing
case free, deterministic, and testable by monkeypatching one stdlib symbol.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.exit_codes import Outcome

#: Wall-clock cap on one probe.
#:
#: Short on purpose and not configurable. A preflight is the thing an
#: operator runs *because* something is wrong, so a probe that blocks on an
#: unreachable daemon for the ambient `command_timeout` (unbounded by
#: default) turns the diagnostic into a second hang. Every probe in this
#: codebase passes it explicitly rather than inheriting `Settings`.
PROBE_TIMEOUT: Final = 5.0


class CheckStatus(StrEnum):
    """How one check came out.

    `SKIPPED` is a real answer, not a missing one: a kubecontext check when
    `kubectl` is absent has nothing to say, and reporting it as a *failure*
    would bill the operator twice for one broken install.
    """

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Check:
    """One preflight result: what was checked, how it went, and what to do.

    `remediation` is `None` only when there is nothing useful to say -- a
    passing check. Every failure carries one, because a preflight that
    reports a problem without a next step is a slower way of running the
    real command and reading its error.
    """

    name: str
    status: CheckStatus
    detail: str
    remediation: str | None = None
    #: What this failure means in `plumbing/exit_codes.py` terms. Always
    #: `SUCCESS` for a passing or skipped check, so the aggregate outcome is
    #: a fold over this field and never a second classification of `status`.
    outcome: Outcome = Outcome.SUCCESS

    @classmethod
    def ok(cls, name: str, detail: str) -> Check:
        """A check that passed."""
        return cls(name=name, status=CheckStatus.OK, detail=detail)

    @classmethod
    def skipped(cls, name: str, detail: str) -> Check:
        """A check that could not be answered, and is not itself a failure."""
        return cls(name=name, status=CheckStatus.SKIPPED, detail=detail)

    @classmethod
    def failed(
        cls,
        name: str,
        detail: str,
        *,
        remediation: str,
        outcome: Outcome,
    ) -> Check:
        """A check that failed, with the fix and what it costs at the exit."""
        return cls(
            name=name,
            status=CheckStatus.FAILED,
            detail=detail,
            remediation=remediation,
            outcome=outcome,
        )

    def to_dict(self) -> dict[str, Any]:
        """The wire shape: name, status, detail, remediation.

        Four keys, fixed. `outcome` is deliberately absent: it is how the
        *process* ends, which the report reports once, and duplicating it
        per row would invite a consumer to fold it themselves and disagree
        with `DoctorReport.outcome` about precedence.
        """
        return {
            "name": self.name,
            "status": str(self.status),
            "detail": self.detail,
            "remediation": self.remediation,
        }


def first_line(text: str) -> str:
    """The first non-empty line of `text`, stripped; "" when there is none.

    The default version parser. Most tools answer `--version` with one line;
    the ones that do not (kubectl's JSON, kyverno's banner) pass their own
    parser to `probe_binary` rather than having this grow special cases.
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

    Three outcomes, and the distinction between the last two is the point:

      * not on PATH        -> `Outcome.MISSING_BINARY` (127). Costs no fork.
      * on PATH, ran badly -> `Outcome.TOOL` (4). It is installed and broken,
                              so "install it" is the wrong advice and a
                              wrapper keying on 127 must not fire.
      * on PATH, ran fine  -> ok, detailed with the version and the path the
                              probe resolved, because "which helm" is half of
                              every real answer when two are installed.

    `version_args=()` means "presence only": some tools (renovate's config
    validator) have no version flag, and inventing one would report a broken
    install for a healthy one.
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
        # Covers CommandTimeout and the MissingToolError a real
        # SubprocessRunner raises when PATH changed between the lookup and
        # the fork. Either way the tool is present but unusable.
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
