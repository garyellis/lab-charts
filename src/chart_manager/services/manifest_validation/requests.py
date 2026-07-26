"""Caller-facing vocabulary of the manifest-validation capability.

What a surface hands in (`SingleRequest`, `RunRequest`), what it gets back
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
    "SingleRequest",
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
class SingleRequest:
    """Validate exactly one chart x env (the render/schema/policy commands).

    Single-row requests do NOT consult `chart-manager.yaml`; callers pass
    values explicitly. `policy_dirs` wins outright when non-empty; when it
    is empty and `discover_policies` is set, the repo-wide and per-chart
    policy directories are discovered. When neither is supplied the policy
    phase sees no paths at all and SKIPs.
    """

    chart: str
    env: str
    root: Path = Path(".")
    values: tuple[Path, ...] = ()
    namespace: str | None = None
    release: str | None = None
    helm_version: str | None = None
    helm_bin: Path | None = None
    kubernetes_version: str | None = None
    schema_locations: tuple[str, ...] = ()
    policy_dirs: tuple[Path, ...] = ()
    discover_policies: bool = False
    out: Path | None = None
    keep: bool = False
    phases: frozenset[str] = ALL_PHASES

    def __post_init__(self) -> None:
        """Reject an unknown phase name and a double helm binding."""
        _check_phases(self.phases)
        if self.helm_version is not None and self.helm_bin is not None:
            raise ValidateInputError(
                "helm_version and helm_bin are mutually exclusive",
                hint="helm_version",
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
