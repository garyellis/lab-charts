"""Transport-neutral entry point for the manifest-validation pipeline.

`ManifestValidationService` is the capability behind `chart-manager validate
chart|run`. It owns the *sequencing* of a run:

  * where the changed-files list comes from (--all > explicit list > git),
  * which helm binding each row runs under (specs may pin a version),
  * how many workers a run gets,
  * how N per-binding sub-runs are stitched into one ordered `RunResult`,
  * the identity (run id) and lifetime (retention rule) of the render dir.

It does NOT own what a row *is*. ``planner.py`` selects the chart/environment
rows, while ``compiler.py`` resolves each target's runtime paths and options.

It owns none of the *appearance*: no `format=`, no `color=`, no console.
Callers get a `RunOutcome` and decide how to render it. Progress narration
is an injected `ProgressDisplay` (the Rich adapters for it live in
`cli/validate_progress.py`) and operator warnings are an injected
`on_warn` callback.

Deliberately Rich-free, like `services/manifest_validation/wire.py`: a REST worker or
Slack handler must be able to import and drive the pipeline without
dragging a TUI library into the process. A guard test in
`tests/test_manifest_validation_rendering.py` asserts it.
"""

from __future__ import annotations

import os
import shutil
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from chart_manager.integrations.git import Git
from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.commands import CommandRunner, SubprocessRunner
from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.services.manifest_validation.catalog import load_manifest_validation_target
from chart_manager.services.manifest_validation.compiler import (
    ResolvedManifestValidation,
    resolve_manifest_validation,
    row_config_for,
)
from chart_manager.services.manifest_validation.models import (
    ALL_PHASES,
    PhaseResult,
    RowResult,
    RunResult,
)
from chart_manager.services.manifest_validation.planner import build_worklist, select_rows
from chart_manager.services.manifest_validation.progress import (
    NullDisplay,
    ProgressDisplay,
    ProgressFinalizer,
)
from chart_manager.services.manifest_validation.requests import (
    RunOutcome,
    RunRequest,
    ValidateInputError,
)
from chart_manager.services.manifest_validation.runner import (
    EventCallback,
    ManifestValidationRunner,
    RowConfig,
    blocked_phase_result,
)
from chart_manager.services.manifest_validation.validator_registry import (
    VALIDATOR_REGISTRY,
)
from chart_manager.services.manifest_validation.validators import (
    ValidatorProvider,
    validate_registry,
)
from chart_manager.settings import (
    DEFAULT_CHARTS_DIR,
    validate_charts_dir,
)

# Re-exports, so a surface needs one import for "drive the validate
# capability": `ALL_PHASES` is defined in `services.manifest_validation.models`, and
# the request/result vocabulary in `.requests`. Those two modules are the
# definitions; this list only spares callers a second import line.
__all__ = [
    "ALL_PHASES",
    "ManifestValidationService",
    "RunOutcome",
    "RunRequest",
    "RunnerSpec",
    "ValidateInputError",
    "default_workers",
    "new_run_id",
    "resolve_workers",
]

WarnCallback = Callable[[str], None]


# --- policies expressed as functions ---------------------------------------


def default_workers() -> int:
    """Auto worker count: max(2, min(cpu_count, 8)).

    The cap keeps memory bounded on beefy CI runners; each worker may hold
    a `helm template` subprocess.
    """
    cpu = os.cpu_count() or 2
    return max(2, min(cpu, 8))


def resolve_workers(requested: int) -> int:
    """Resolve the requested worker count (0 = auto, else at least 1)."""
    return default_workers() if requested == 0 else max(1, requested)


def new_run_id() -> str:
    """Mint a run id: UTC timestamp + a short uuid suffix."""
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


# --- runner construction ---------------------------------------------------


@dataclass(frozen=True)
class RunnerSpec:
    """Everything needed to build one `ManifestValidationRunner`.

    One spec per distinct helm binding: specs may pin `helm_version` (or an
    explicit binary) per chart, and each binding needs its own `Helm` (the
    per-chart `dependency update` dedupe lives on the instance).
    """

    output_root: Path
    helm_version: str | None = None
    helm_bin: str | Path | None = None
    max_workers: int = 1
    on_event: EventCallback | None = None
    row_timeout: float | None = None
    dep_update_timeout: float | None = 300.0
    verbose: bool = True
    validator_ids: frozenset[str] = frozenset(
        provider.validator_id for provider in VALIDATOR_REGISTRY
    )


RunnerFactory = Callable[[RunnerSpec], ManifestValidationRunner]


# --- the app ---------------------------------------------------------------


class ManifestValidationService:
    """Run the validate pipeline for any surface.

    Construction is injectable end to end so the whole pipeline can be
    driven in a unit test with no subprocesses and no Typer: pass a
    `runner_factory` for the phase execution, `git_factory` for the
    changed-files source, and `run_id_factory` for a deterministic out dir.
    """

    def __init__(
        self,
        *,
        progress: ProgressDisplay | None = None,
        on_warn: WarnCallback | None = None,
        runner_factory: RunnerFactory | None = None,
        command_runner: CommandRunner | None = None,
        git_factory: Callable[[Path], Git] | None = None,
        run_id_factory: Callable[[], str] | None = None,
        charts_dir: Path = DEFAULT_CHARTS_DIR,
        validator_providers: tuple[ValidatorProvider, ...] = VALIDATOR_REGISTRY,
    ) -> None:
        """Wire the progress sink, warning channel, and construction hooks."""
        self._charts_dir = validate_charts_dir(charts_dir)
        self._progress: ProgressDisplay = progress or NullDisplay()
        # No-op default so call sites can warn unconditionally.
        self._on_warn: WarnCallback = on_warn or (lambda _msg: None)
        self._runner_factory: RunnerFactory = runner_factory or self._build_runner
        self._command_runner = command_runner or SubprocessRunner()
        self._git_factory = git_factory or (lambda root: Git(root, charts_dir=self._charts_dir))
        self._run_id_factory = run_id_factory or new_run_id
        self._validator_providers = validate_registry(validator_providers)

    # --- spec-driven run ---------------------------------------------------

    def run(self, request: RunRequest) -> RunOutcome:
        """Build the worklist from specs + git, run every row, aggregate.

        Raises `ValidateInputError` when an explicit changed-files list
        cannot be read. A failed `git diff` is NOT fatal: it downgrades to
        a warning and falls back to validating everything.
        """
        repo_root = request.root.resolve()

        changed = self._resolve_changed_files(repo_root, request)
        build = build_worklist(
            root=repo_root,
            changed_files=changed,
            skip_change_detection=request.skip_change_detection,
            selected_charts=(
                request.charts if request.charts and changed is None else ()
            ),
            charts_dir=self._charts_dir,
        )

        selection = select_rows(
            build.rows,
            charts=set(request.charts),
            envs=set(request.envs),
            available_charts=set(build.targets),
            available_environments={
                environment
                for target in build.targets.values()
                for environment in target.spec.environments
            },
            ignored_changes=build.ignored_changes,
            unmatched_changes=build.unmatched_changes,
            warnings=build.warnings,
        )
        # A repository scan intentionally excludes unconfigured and disabled
        # capabilities. For an explicit chart filter, resolve each excluded
        # name strictly so the caller sees the real configuration state
        # instead of the misleading "unknown chart" fallback. Malformed
        # configuration remains a result-level spec error (exit 3).
        explicit_spec_errors: list[str] = []
        unresolved_charts: list[str] = []
        for chart_name in selection.unmatched_charts:
            try:
                load_manifest_validation_target(
                    repo_root,
                    chart_name,
                    charts_dir=self._charts_dir,
                )
            except SpecError as exc:
                # Repository discovery already records malformed present
                # config with the chart name. Keep one diagnostic while
                # still distinguishing it from unavailable capability state.
                if not any(error.startswith(f"{chart_name}: ") for error in build.spec_errors):
                    explicit_spec_errors.append(str(exc))
            except ChartManagerError as exc:
                raise ValidateInputError(str(exc), hint="charts") from exc
            else:
                unresolved_charts.append(chart_name)
        if unresolved_charts:
            raise ValidateInputError(
                "unknown chart filter(s): " + ", ".join(unresolved_charts),
                hint="charts",
            )
        if selection.unmatched_environments and not explicit_spec_errors:
            raise ValidateInputError(
                "unknown environment filter(s): " + ", ".join(selection.unmatched_environments),
                hint="envs",
            )
        rows = selection.rows
        filtered_out = selection.filtered_out

        out_dir, keep = self._resolve_out_dir(repo_root, request.out, request.keep)

        # Specs may pin a helm version per chart; group rows by their helm
        # binding so we build one runner (and one Helm) per distinct
        # binding rather than one per row.
        grouped: dict[tuple[str | None, str | None], list[RowConfig]] = {}
        compiled_by_chart: dict[str, ResolvedManifestValidation] = {}
        compile_warnings: list[str] = []
        for row in rows:
            # Indexed, not `.get(...) or continue`: `build_worklist` only
            # materializes rows for charts in `build.targets`, and selection
            # only removes rows. A miss here means that
            # invariant broke, and a silent `continue` would drop the row
            # from the run without it appearing anywhere in the result.
            target = build.targets[row.chart]
            spec = target.spec
            if row.chart not in compiled_by_chart:
                compiled = resolve_manifest_validation(
                    target,
                    repo_root,
                    providers=self._validator_providers,
                )
                compiled_by_chart[row.chart] = compiled
                compile_warnings.extend(compiled.warnings)
                for warning in compiled.warnings:
                    self._on_warn(warning)
            grouped.setdefault((spec.helm_version, spec.helm_binary), []).append(
                row_config_for(compiled_by_chart[row.chart], row)
            )

        workers = resolve_workers(request.workers)
        # Streamed subprocess output from >1 worker interleaves into
        # illegible noise, defeating the point of verbose mode (debugging
        # hangs), so verbose runs are serial.
        if request.verbose:
            workers = 1

        base_spec = RunnerSpec(
            output_root=out_dir,
            max_workers=workers,
            # 0 means unbounded at the request boundary; the runner and the
            # integrations below it want None as that sentinel.
            row_timeout=request.row_timeout if request.row_timeout > 0 else None,
            dep_update_timeout=(
                request.dep_update_timeout if request.dep_update_timeout > 0 else None
            ),
            verbose=request.verbose,
        )

        all_cfgs = [cfg for cfgs in grouped.values() for cfg in cfgs]
        progress = ProgressFinalizer(self._progress)
        base_spec = replace(base_spec, on_event=progress.on_event)
        self._progress.start([cfg.row for cfg in all_cfgs])
        aggregated: list[RowResult] = []
        stopped = False
        try:
            for (helm_version, helm_bin), cfgs in grouped.items():
                if stopped:
                    group_rows = tuple(self._not_run(cfg) for cfg in cfgs)
                else:
                    try:
                        runner = self._runner_factory(
                            replace(
                                base_spec,
                                helm_version=helm_version,
                                helm_bin=helm_bin,
                                validator_ids=frozenset(
                                    invocation.validator_id
                                    for cfg in cfgs
                                    for invocation in cfg.validator_invocations
                                    if invocation.enabled
                                    and invocation.category.value
                                    in request.phases
                                ),
                            )
                        )
                        group_rows = runner.run(
                            cfgs,
                            enabled_phases=request.phases,
                            fail_fast=request.fail_fast,
                        ).rows
                    except Exception as exc:
                        group_rows = tuple(
                            self._execution_failure(cfg, exc, request.phases)
                            for cfg in cfgs
                        )
                aggregated.extend(group_rows)
                for row_result in group_rows:
                    progress.finalize(row_result)
                if request.fail_fast and any(
                    phase.status == "FAIL"
                    for row_result in group_rows
                    for phase in row_result.phases.values()
                ):
                    stopped = True
        finally:
            self._progress.stop()

        # Each sub-run is internally sorted; re-sort the union so output is
        # deterministic across helm-binding groups too.
        aggregated.sort(key=lambda r: (r.row.chart, r.row.env))

        return RunOutcome(
            result=RunResult(
                rows=tuple(aggregated),
                rendered_root=out_dir,
                spec_errors=tuple(dict.fromkeys((*build.spec_errors, *explicit_spec_errors))),
            ),
            out_dir=out_dir,
            keep=keep,
            warnings=(*selection.warnings, *compile_warnings),
            ignored_changes=selection.ignored_changes,
            unmatched_changes=selection.unmatched_changes,
            unmatched_charts=selection.unmatched_charts,
            unmatched_environments=selection.unmatched_environments,
            charts_unvalidated=build.chart_count_unvalidated,
            rows_filtered_out=filtered_out,
            enabled_phases=request.phases,
        )

    # --- artifact lifetime -------------------------------------------------

    def cleanup(self, outcome: RunOutcome) -> None:
        """Delete the render dir unless it is being kept; never raises.

        Retained on an explicit keep, on any non-zero exit code (the
        artifacts are the evidence), or when DEBUG=true. Callers invoke
        this once they are done reading the artifacts — emitting a summary
        into the render dir has to happen first.
        """
        if outcome.keep or outcome.exit_code != 0:
            return
        if os.environ.get("DEBUG", "").lower() == "true":
            return
        if not outcome.out_dir.exists():
            return
        try:
            shutil.rmtree(outcome.out_dir)
        except OSError as exc:
            self._on_warn(f"warning: cleanup failed: {exc}")

    # --- internals ---------------------------------------------------------

    def _resolve_out_dir(self, repo_root: Path, out: Path | None, keep: bool) -> tuple[Path, bool]:
        """Resolve the render dir; an explicit dir is an implicit keep.

        A caller who names the directory chose it deliberately, so we never
        surprise-delete it.
        """
        if out is not None:
            return out.resolve(), True
        run_id = self._run_id_factory()
        return (repo_root / ".chart-manager" / "rendered" / run_id).resolve(), keep

    def _resolve_changed_files(self, repo_root: Path, request: RunRequest) -> list[str] | None:
        """Resolve the changed-files list; None means "validate everything".

        Precedence: skip_change_detection > an explicit changed-files file > explicit
        chart selection > `git diff` against `base`. A plain ``--chart`` is
        an intentional request to validate that chart, not a filter over an
        unrelated Git diff. A failed git diff is a warning, not an error: a
        shallow CI checkout or a missing base ref must widen the run rather
        than fail it.
        """
        if request.skip_change_detection:
            return None
        if request.changed_files is not None:
            try:
                text = request.changed_files.read_text()
            except OSError as exc:
                raise ValidateInputError(
                    f"cannot read changed-files list: {exc}", hint="changed_files"
                ) from exc
            return [line for line in text.splitlines() if line.strip()]
        if request.charts:
            return None
        try:
            return self._git_factory(repo_root).changed_files(base=request.base)
        except ChartManagerError as exc:
            self._on_warn(f"warn: git diff failed ({exc}); falling back to --all")
            return None

    def _build_runner(self, spec: RunnerSpec) -> ManifestValidationRunner:
        """Default runner factory: one Helm per binding, shared command runner."""
        validators = {
            provider.validator_id: provider.build(
                command_runner=self._command_runner,
                timeout=spec.row_timeout,
            )
            for provider in self._validator_providers
            if provider.validator_id in spec.validator_ids
        }
        return ManifestValidationRunner(
            helm=Helm(
                runner=self._command_runner,
                version=spec.helm_version,
                binary=spec.helm_bin,
                verbose=spec.verbose,
            ),
            output_root=spec.output_root,
            validators=validators,
            max_workers=spec.max_workers,
            on_event=spec.on_event,
            row_timeout=spec.row_timeout,
            dep_update_timeout=spec.dep_update_timeout,
        )

    @staticmethod
    def _execution_failure(
        cfg: RowConfig,
        exc: Exception,
        active: frozenset[str],
    ) -> RowResult:
        """Convert a runner-level failure into a complete terminal row.

        This is the boundary for failures outside an individual validation
        phase: runner/Helm construction, dependency prefetch, and unexpected
        serial runner crashes. A failure in one Helm-binding group therefore
        remains visible without preventing later independent groups from
        running.
        """
        rendered = traceback.format_exception_only(type(exc), exc)
        detail = (rendered[-1] if rendered else repr(exc)).strip()

        return RowResult(
            row=cfg.row,
            phases={
                "render": PhaseResult(
                    phase="render",
                    status="FAIL",
                    detail=f"execution failed: {detail}",
                    error_type="tool",
                ),
                "schema": blocked_phase_result(
                    cfg, active, "schema", upstream="render"
                ),
                "policy": blocked_phase_result(
                    cfg, active, "policy", upstream="render"
                ),
            },
        )

    @staticmethod
    def _not_run(cfg: RowConfig) -> RowResult:
        """Represent a row omitted after an earlier fail-fast failure."""
        return RowResult(
            row=cfg.row,
            phases={
                "render": PhaseResult(
                    phase="render",
                    status="NOT_RUN",
                    detail="fail-fast: earlier row failed",
                ),
                "schema": PhaseResult(
                    phase="schema",
                    status="NOT_RUN",
                    detail="fail-fast: earlier row failed",
                ),
                "policy": PhaseResult(
                    phase="policy",
                    status="NOT_RUN",
                    detail="fail-fast: earlier row failed",
                ),
            },
        )
