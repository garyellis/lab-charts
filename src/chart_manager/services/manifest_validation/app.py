"""Transport-neutral entry point for the manifest-validation pipeline.

`ManifestValidationService` is the capability behind `chart-manager chart
validate`. It owns the *sequencing* of a run:

  * where the changed-files list comes from (--all > explicit list > git),
  * how many workers a run gets,
  * the identity (run id), contents (`summary.md`/`summary.json`) and
    lifetime (retention rule) of the render dir.

It does NOT own execution. One `ManifestValidationRunner` receives the whole
worklist and owns fan-out, fail-fast, row ordering and progress dedupe. This
module used to partition rows by helm binding and reimplement all four on top
of it, because a runner bound one `Helm`; the binding now travels on the row.

It does NOT own what a row *is*. ``planner.py`` selects the chart/environment
rows, while ``resolver.py`` resolves each target's runtime paths and options.

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

import json
import os
import shutil
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from chart_manager.integrations.git import Git
from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.commands import CommandRunner, SubprocessRunner
from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.services.manifest_validation.catalog import load_manifest_validation_target
from chart_manager.services.manifest_validation.markdown import to_markdown
from chart_manager.services.manifest_validation.models import (
    ALL_PHASES,
    PHASE_ORDER,
    RunOutcome,
    RunRequest,
    RunResult,
    ValidateInputError,
)
from chart_manager.services.manifest_validation.paths import RENDER_OUTPUT_DIR
from chart_manager.services.manifest_validation.planner import build_worklist, select_rows
from chart_manager.services.manifest_validation.progress import (
    NullDisplay,
    ProgressDisplay,
)
from chart_manager.services.manifest_validation.resolver import (
    ResolvedManifestValidation,
    resolve_manifest_validation,
    row_config_for,
)
from chart_manager.services.manifest_validation.runner import (
    EventCallback,
    ManifestValidationRunner,
    RowConfig,
    crash_row,
)
from chart_manager.services.manifest_validation.validator_adapters import (
    VALIDATOR_REGISTRY,
)
from chart_manager.services.manifest_validation.validators import (
    ValidatorProvider,
    validate_registry,
)
from chart_manager.services.manifest_validation.wire import to_json
from chart_manager.settings import (
    DEFAULT_CHARTS_DIR,
    validate_charts_dir,
)

# Re-exports, so a surface needs one import for "drive the validate
# capability": `ALL_PHASES` and the request/result vocabulary are defined in
# `services.manifest_validation.models`. That module is the definition; this
# list only spares callers a second import line.
__all__ = [
    "ALL_PHASES",
    "ManifestValidationService",
    "RunOutcome",
    "RunRequest",
    "RunnerSpec",
    "ValidateInputError",
    "default_workers",
    "new_run_id",
    "resolve_phases",
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


def resolve_phases(
    requested: Sequence[str],
    *,
    kubeconform: bool = True,
    policy: bool = True,
) -> frozenset[str]:
    """Resolve a surface's phase selection into the set the runner executes.

    An empty `requested` means "not given", which is all three phases.
    Expressing the default as absence rather than as a literal list is what
    lets `kubeconform=False` / `policy=False` stay *subtractive*: they narrow
    whatever was selected instead of replacing it, so at the default they
    reproduce `{render} + schema? + policy?` and still compose with an
    explicit selection.

    Rejects unknown and blank names here rather than leaving them to
    `RunRequest.__post_init__`, so a surface can map `hint="phases"` onto its
    own input name (a flag, a JSON field) before a run starts. Subtracting
    down to the empty set is deliberately *not* rejected here -- that is the
    request's invariant, and it holds for every caller, not just this one.
    """
    parts = {value.strip() for value in requested if value.strip()}
    if not parts:
        if requested:
            # Given, but nothing but blanks. Silently falling back to "all
            # phases" would run more work than the caller asked for and
            # report success for phases they tried to exclude.
            raise ValidateInputError("--phase needs a phase name", hint="phases")
        parts = set(ALL_PHASES)
    unknown = parts - ALL_PHASES
    if unknown:
        raise ValidateInputError(
            # PHASE_ORDER, not sorted(ALL_PHASES): show the phases in the
            # order a caller would type them, which is also the order the
            # CLI's help text uses.
            f"unknown phase(s): {', '.join(sorted(unknown))}; valid: {','.join(PHASE_ORDER)}",
            hint="phases",
        )
    if not kubeconform:
        parts -= {"schema"}
    if not policy:
        parts -= {"policy"}
    return frozenset(parts)


def new_run_id() -> str:
    """Mint a run id: UTC timestamp + a short uuid suffix."""
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


# --- runner construction ---------------------------------------------------


@dataclass(frozen=True)
class RunnerSpec:
    """Everything needed to build the one `ManifestValidationRunner` a run uses.

    One spec per run, not per helm binding. The binding travels on each
    `RowConfig` and the runner builds a `Helm` per distinct binding, so a
    heterogeneous batch needs exactly one runner -- which is what keeps
    fan-out, fail-fast and ordering from being reimplemented above it.
    """

    output_root: Path
    max_workers: int = 1
    on_event: EventCallback | None = None
    tool_timeout: float | None = None
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

        compiled_by_chart: dict[str, ResolvedManifestValidation] = {}
        compile_warnings: list[str] = []
        configs: list[RowConfig] = []
        for row in rows:
            # Indexed, not `.get(...) or continue`: `build_worklist` only
            # materializes rows for charts in `build.targets`, and selection
            # only removes rows. A miss here means that
            # invariant broke, and a silent `continue` would drop the row
            # from the run without it appearing anywhere in the result.
            target = build.targets[row.chart]
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
            configs.append(row_config_for(compiled_by_chart[row.chart], row))

        workers = resolve_workers(request.workers)
        # Streamed subprocess output from >1 worker interleaves into
        # illegible noise, defeating the point of verbose mode (debugging
        # hangs), so verbose runs are serial.
        if request.verbose:
            workers = 1

        # ONE runner for the whole batch. Specs may pin a helm version per
        # chart, but each row carries its own binding and the runner memoizes
        # a Helm per binding, so fan-out, fail-fast, ordering and event dedupe
        # all stay in the one place that already implements them. Grouping
        # rows by binding here used to force a second copy of each of those,
        # and made a pinned chart wait for every unpinned one to finish.
        spec = RunnerSpec(
            output_root=out_dir,
            max_workers=workers,
            on_event=self._progress.on_event,
            # 0 means unbounded at the request boundary; the runner and the
            # integrations below it want None as that sentinel.
            tool_timeout=request.tool_timeout if request.tool_timeout > 0 else None,
            dep_update_timeout=(
                request.dep_update_timeout if request.dep_update_timeout > 0 else None
            ),
            verbose=request.verbose,
            validator_ids=frozenset(
                invocation.validator_id
                for cfg in configs
                for invocation in cfg.validator_invocations
                if invocation.enabled and invocation.category.value in request.phases
            ),
        )

        self._progress.start([cfg.row for cfg in configs])
        try:
            executed = self._runner_factory(spec).run(
                configs,
                enabled_phases=request.phases,
                fail_fast=request.fail_fast,
            ).rows
        except Exception as exc:
            # The runner could not be built, or died before it could turn
            # anything into a row. Every other failure boundary -- an unusable
            # helm binding, a failed prefetch, a crashed worker -- belongs to
            # the runner, which reports those as rows itself. Progress is
            # narrated here for the same reason: nothing else saw these rows.
            executed = tuple(
                crash_row(cfg, exc, active=request.phases, context="execution failed")
                for cfg in configs
            )
            for row_result in executed:
                for phase in row_result.phases.values():
                    self._progress.on_event(
                        row_result.row,
                        phase.phase,
                        phase.status,
                        phase.elapsed_seconds,
                    )
        finally:
            self._progress.stop()

        return RunOutcome(
            result=RunResult(
                rows=executed,
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
            workers=workers,
        )

    # --- artifacts ---------------------------------------------------------

    def write_summaries(
        self,
        outcome: RunOutcome | RunResult,
        *,
        out_dir: Path,
        include_timings: bool = False,
        requested_charts: tuple[str, ...] = (),
        requested_environments: tuple[str, ...] = (),
    ) -> str:
        """Write `summary.md` and `summary.json` into `out_dir`; return the markdown.

        The CLI used to compose and write these itself, which made the whole
        `--output all` artifact set unreachable from any second surface while
        `cli/validate.py`'s docstring claimed retention lived here.

        Returns the markdown it wrote so a caller with a second sink -- the
        `$GITHUB_STEP_SUMMARY` append is the one in this repo, and it is
        genuinely runner-specific plumbing -- emits the same bytes rather
        than rendering the run twice.

        Best effort: a failed write warns through `on_warn` and does not
        raise. The rendered tree may legitimately be gone by now, and losing
        a summary must not change the verdict of the run it summarizes.
        """
        markdown = to_markdown(
            outcome,
            include_timings=include_timings,
            requested_charts=requested_charts,
            requested_environments=requested_environments,
        )
        payload = (
            json.dumps(
                to_json(
                    outcome,
                    requested_charts=requested_charts,
                    requested_environments=requested_environments,
                ),
                indent=2,
            )
            + "\n"
        )
        for filename, text in (("summary.md", markdown), ("summary.json", payload)):
            sidecar = out_dir / filename
            try:
                sidecar.parent.mkdir(parents=True, exist_ok=True)
                sidecar.write_text(text)
            except OSError as exc:
                self._on_warn(f"warning: could not write {sidecar}: {exc}")
        return markdown

    # --- artifact lifetime -------------------------------------------------

    def cleanup(self, outcome: RunOutcome) -> None:
        """Delete the render dir unless it is being kept; never raises.

        Retained on an explicit keep, on anything but a clean run (the
        artifacts are the evidence), or when DEBUG=true. Callers invoke
        this once they are done reading the artifacts — emitting a summary
        into the render dir has to happen first.
        """
        if outcome.keep or not outcome.ok:
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
        return (repo_root / RENDER_OUTPUT_DIR / run_id).resolve(), keep

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
        """Default runner factory: one memoized Helm per binding, shared command runner.

        The validator executors are built for the union of every enabled
        validator in the batch. That is a superset for any single row, which
        is the safe direction: the runner only invokes a validator a row's own
        invocations enable, and a missing executor is a row FAIL.
        """
        validators = {
            provider.validator_id: provider.build(
                command_runner=self._command_runner,
                timeout=spec.tool_timeout,
            )
            for provider in self._validator_providers
            if provider.validator_id in spec.validator_ids
        }
        return ManifestValidationRunner(
            helm_factory=lambda version, binary: Helm(
                runner=self._command_runner,
                version=version,
                binary=binary,
                verbose=spec.verbose,
            ),
            output_root=spec.output_root,
            validators=validators,
            max_workers=spec.max_workers,
            on_event=spec.on_event,
            tool_timeout=spec.tool_timeout,
            dep_update_timeout=spec.dep_update_timeout,
        )
