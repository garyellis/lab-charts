"""Caller-facing vocabulary of the manifest-validation capability.

What a surface hands in (`RunRequest`), what it gets back
(`RunOutcome`), and the one error type that says "your input was bad, and
here is which input" (`ValidateInputError`).

Separate from `app.py` because these are the *contract*, not the
implementation: a REST handler deserializes into these and never needs to
import the orchestrator. Both requests validate themselves in
`__post_init__`, so an ill-formed request cannot reach `ManifestValidationService` — the
rule has one owner, and the surface's job is only to map `hint` onto
whatever it calls that input (a flag, a JSON field).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.manifest_validation.models import ALL_PHASES, RunResult

__all__ = [
    "RunOutcome",
    "RunRequest",
    "ValidateInputError",
]


class ValidateInputError(ChartManagerError):
    """A caller-supplied validate input could not be resolved.

    `hint` names the offending input so a surface can point at the right
    flag (or JSON field) without string-matching the message.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        """Store the message plus the name of the input that was rejected."""
        super().__init__(message)
        self.hint = hint


def _check_phases(phases: frozenset[str]) -> None:
    """Reject an empty or unknown phase set."""
    if not phases:
        raise ValidateInputError("at least one phase must be enabled", hint="phases")
    unknown = phases - ALL_PHASES
    if unknown:
        raise ValidateInputError(
            f"unknown phase(s): {', '.join(sorted(unknown))}; "
            f"valid: {', '.join(sorted(ALL_PHASES))}",
            hint="phases",
        )


@dataclass(frozen=True)
class RunRequest:
    """Spec-driven multi-row run (`validate run`).

    `charts`/`envs` narrow the built worklist. `changed_files` (a file of
    newline-delimited paths) and `base` (a git ref) feed the changed-files
    resolution; `all_charts` short-circuits both. Timeouts use the
    pipeline's 0-means-unbounded convention. ``fail_fast`` stops before
    preparing later independent rows after the first failure.
    """

    root: Path = Path(".")
    charts: tuple[str, ...] = ()
    envs: tuple[str, ...] = ()
    base: str = "origin/main"
    changed_files: Path | None = None
    all_charts: bool = False
    phases: frozenset[str] = ALL_PHASES
    out: Path | None = None
    keep: bool = False
    workers: int = 0
    verbose: bool = False
    row_timeout: float = 0.0
    dep_update_timeout: float = 300.0
    fail_fast: bool = False

    def __post_init__(self) -> None:
        """Reject an unknown phase name."""
        _check_phases(self.phases)


@dataclass(frozen=True)
class RunOutcome:
    """Everything one validate run produced.

    `result` is the wire-projectable payload (`services/manifest_validation/wire.py`);
    the remaining fields are run metadata a surface may want to narrate —
    non-fatal build warnings, how many charts carried no spec, how many
    rows the chart/env filters dropped — plus the artifact lifetime inputs
    `cleanup()` needs.
    """

    result: RunResult
    out_dir: Path
    keep: bool = False
    warnings: tuple[str, ...] = ()
    ignored_changes: tuple[Path, ...] = ()
    unmatched_changes: tuple[Path, ...] = ()
    unmatched_charts: tuple[str, ...] = ()
    unmatched_environments: tuple[str, ...] = ()
    charts_unvalidated: int = 0
    rows_filtered_out: int = 0
    enabled_phases: frozenset[str] = ALL_PHASES

    @property
    def exit_code(self) -> int:
        """Process-style exit code folded from the underlying RunResult."""
        return self.result.exit_code()

    @property
    def ok(self) -> bool:
        """True when nothing failed (exit code 0)."""
        return self.exit_code == 0
