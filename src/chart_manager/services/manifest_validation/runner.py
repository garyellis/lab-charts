"""Manifest-validation pipeline runner.

Sequences render -> schema -> policy per row. Strict per-row dependency:
a render FAIL short-circuits both downstream phases to SKIP; a schema FAIL
short-circuits policy to SKIP. Across rows the default is fail-fast false,
so every independent row is attempted and the operator sees the full failure
surface in one run. Callers may opt into truthful fail-fast execution, which
stops before later rows start.

Rows are independent: with max_workers > 1 they execute concurrently via a
ThreadPoolExecutor. The per-row sequencing above is preserved inside each
worker. Phase functions (and the integrations they call) are responsible
for their own thread-safety; Helm's per-chart `dependency update` dedupe
is the load-bearing example.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.errors import SpecError
from chart_manager.services.manifest_validation import phases
from chart_manager.services.manifest_validation.models import (
    ALL_PHASES,
    ErrorType,
    PhaseName,
    PhaseResult,
    PhaseStatus,
    RowResult,
    RunResult,
    WorklistRow,
)
from chart_manager.services.manifest_validation.output_paths import (
    case_output_directory,
    reset_case_output_directory,
)
from chart_manager.services.manifest_validation.validators import (
    ManifestValidator,
    ValidatorCategory,
    ValidatorInvocation,
)

EventCallback = Callable[[WorklistRow, str, str, float | None], None]


@dataclass(frozen=True)
class RowConfig:
    """Per-row inputs threaded through every phase.

    One config for all phases rather than one input struct per phase, so
    adding a phase does not multiply the constructor surface.

    ``row_config_for`` assembles these from a chart's
    ``chart-lifecycle.yaml`` for both ``validate chart`` and ``validate run``.
    """

    row: WorklistRow
    chart_path: Path
    validator_invocations: tuple[ValidatorInvocation, ...]
    values: list[Path] = field(default_factory=list)

    def invocations_for(self, category: ValidatorCategory) -> tuple[ValidatorInvocation, ...]:
        """Return this row's invocations for one gate in deterministic order."""
        return tuple(
            sorted(
                (
                    invocation
                    for invocation in self.validator_invocations
                    if invocation.category is category
                ),
                key=lambda invocation: invocation.order,
            )
        )


def blocked_phase_result(
    cfg: RowConfig,
    active: frozenset[str],
    phase: PhaseName,
    *,
    upstream: str,
) -> PhaseResult:
    """Describe why a downstream phase cannot run, in precedence order."""
    if phase not in active:
        return PhaseResult(phase=phase, status="NOT_RUN")
    category = ValidatorCategory(phase)
    if not any(invocation.enabled for invocation in cfg.invocations_for(category)):
        return PhaseResult(
            phase=phase,
            status="SKIP",
            detail="disabled by chart-lifecycle",
            skip_cause="validator_disabled",
        )
    return PhaseResult(
        phase=phase,
        status="SKIP",
        detail=f"upstream {upstream} FAIL",
        skip_cause="upstream_failed",
    )


class ManifestValidationRunner:
    """Orchestrate render -> schema -> policy across worklist rows, optionally in parallel."""

    def __init__(
        self,
        *,
        helm: Helm,
        output_root: Path,
        validators: dict[str, ManifestValidator],
        max_workers: int = 1,
        on_event: EventCallback | None = None,
        dep_update_timeout: float | None = 300.0,
        row_timeout: float | None = None,
    ) -> None:
        """Wire integrations, worker count, event callback, and dep/row timeouts."""
        self.helm = helm
        self.validators = dict(validators)
        self.output_root = output_root.resolve()
        self.max_workers = max(1, max_workers)
        # No-op default so phase code can fire-and-forget without a None
        # check on every event. CLI wires in a real callback.
        self.on_event: EventCallback = on_event or (lambda *_args: None)
        self._terminal_events: set[tuple[WorklistRow, str]] = set()
        self._event_lock = Lock()
        # 5-min default guards prefetch against hung OCI/DNS lookups. The
        # CLI exposes --dep-update-timeout for override (None = unbounded).
        self.dep_update_timeout = dep_update_timeout
        # Per-row hard cap on total wall-clock for ALL phases in one row.
        # Default None preserves legacy behavior; CLI exposes --row-timeout.
        # On timeout, the row is marked FAIL with error_type=tool.
        self.row_timeout = row_timeout
        # Propagate the cap before execution. Helm is shared across
        # `max_workers` threads, so mutating it after fan-out would race.
        # Validator providers receive the same timeout while constructing
        # their executors in the service composition root.
        if self.row_timeout is not None:
            self.helm.timeout = self.row_timeout

    def run(
        self,
        configs: list[RowConfig],
        *,
        enabled_phases: frozenset[str] | None = None,
        fail_fast: bool = False,
    ) -> RunResult:
        """Execute render -> schema -> policy across rows.

        `enabled_phases` (default: all three) restricts which phases run;
        disabled phases get a `NOT_RUN` PhaseResult instead. Disabling a
        phase does NOT short-circuit later phases — schema-only runs still
        render first because schema needs the rendered tree. With
        ``fail_fast=True``, execution is serial and later rows become
        ``NOT_RUN`` after the first failed row.
        """
        if not configs:
            return RunResult(rows=(), rendered_root=self.output_root)

        active = enabled_phases if enabled_phases is not None else ALL_PHASES

        # Validate the complete batch before dependency prefetch or any
        # filesystem mutation. Re-check immediately before each reset to
        # guard against a path component being replaced by a symlink between
        # planning and execution.
        for cfg in configs:
            target = case_output_directory(
                self.output_root,
                chart=cfg.row.chart,
                environment=cfg.row.env,
            )
            if target.exists() and not target.is_dir():
                raise SpecError(f"validation output path is not a directory: {target}")

        # Pre-fetch helm dependencies once per distinct chart before fanning
        # out per-row work. Without this, N envs of the same chart all hit
        # Helm.dependency_update serialized on the per-chart lock — the
        # first call holds the lock for the entire network fetch while the
        # other N-1 wait. Pre-fetching collapses that wait to a single up-
        # front pass that can itself parallelize across DISTINCT charts.
        # NOTE: `_run_row` renders unconditionally (schema and policy need
        # the tree), so this gate is narrower than the work it guards: a
        # `--phase schema` run renders WITHOUT the prefetch and each row
        # pays its own first-time dep fetch serially. Left as-is because
        # widening it adds `helm dependency update` subprocesses to a run
        # that does not ask for them today; fix it deliberately, with a
        # timing test, rather than as a side effect.
        with self._event_lock:
            self._terminal_events.clear()

        prefetch_failures = (
            self._prefetch_dependencies(configs, fail_fast=fail_fast) if "render" in active else {}
        )
        results: list[RowResult] = []
        runnable: list[RowConfig] = []
        stopped = False
        for cfg in configs:
            exc = prefetch_failures.get(cfg.chart_path.resolve())
            if stopped:
                results.append(self._not_run_row(cfg))
            elif exc is not None:
                results.append(
                    self._crash_row(
                        cfg,
                        exc,
                        active=active,
                        context="dependency prefetch failed",
                    )
                )
                stopped = fail_fast
            else:
                runnable.append(cfg)

        # Fail-fast execution is intentionally serial. It gives the flag a
        # truthful boundary: once one row fails, no later row has already
        # started in another worker.
        if self.max_workers == 1 or fail_fast:
            for index, cfg in enumerate(runnable):
                if stopped:
                    results.append(self._not_run_row(cfg))
                    continue
                try:
                    row_result = self._run_row(cfg, active)
                except Exception as exc:
                    row_result = self._crash_row(cfg, exc, active=active)
                results.append(row_result)
                if fail_fast and self._row_failed(row_result):
                    stopped = True
                    for remaining in runnable[index + 1 :]:
                        results.append(self._not_run_row(remaining))
                    break
        elif runnable:
            with ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="validate-",
            ) as pool:
                futures = {pool.submit(self._run_row, cfg, active): cfg for cfg in runnable}
                for fut in as_completed(futures):
                    cfg = futures[fut]
                    try:
                        results.append(fut.result())
                    except Exception as exc:
                        # Worker crashed outside a phase function — convert
                        # to a visible row failure so the cross-row
                        # fail-fast=false invariant holds. We deliberately
                        # do NOT catch BaseException: KeyboardInterrupt /
                        # SystemExit must propagate so Ctrl-C terminates a
                        # long parallel run instead of being absorbed into
                        # a per-row "FAIL".
                        results.append(self._crash_row(cfg, exc, active=active))

        for result in results:
            self._finalize_row(result)

        # Deterministic output order regardless of completion order.
        results.sort(key=lambda r: (r.row.chart, r.row.env))
        return RunResult(rows=tuple(results), rendered_root=self.output_root)

    def _prefetch_dependencies(
        self,
        configs: list[RowConfig],
        *,
        fail_fast: bool,
    ) -> dict[Path, Exception]:
        """Run `helm dependency update` once per distinct chart path.

        Helm.dependency_update is already idempotent (per-instance lock +
        dedupe set), so this is technically redundant — but doing the
        prefetch BEFORE the worker fan-out means no row blocks on another
        row's first-time dep fetch. Parallelizes across distinct charts at
        the same worker count as the main pool.
        """
        distinct_charts: list[Path] = []
        seen: set[Path] = set()
        for cfg in configs:
            resolved = cfg.chart_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            distinct_charts.append(cfg.chart_path)

        if not distinct_charts:
            return {}

        def _update(chart_path: Path) -> None:
            """Prefetch one chart's helm dependencies."""
            self.helm.dependency_update(chart_path, timeout=self.dep_update_timeout)

        failures: dict[Path, Exception] = {}
        if self.max_workers == 1 or len(distinct_charts) == 1 or fail_fast:
            for chart_path in distinct_charts:
                try:
                    _update(chart_path)
                except Exception as exc:
                    failures[chart_path.resolve()] = exc
                    if fail_fast:
                        break
            return failures
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(distinct_charts)),
            thread_name_prefix="validate-deps-",
        ) as pool:
            futures = {pool.submit(_update, path): path for path in distinct_charts}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    failures[futures[future].resolve()] = exc
        return failures

    def _run_row(self, cfg: RowConfig, active: frozenset[str]) -> RowResult:
        """Run the enabled phases for one row in order; render precedes schema/policy.

        Render is unconditional. Per `run`'s contract, disabling a phase
        does not short-circuit later ones — schema and policy both read the
        rendered tree, so `--phase schema` still has to render first. There
        used to be an `if any phase is active` guard here plus a matching
        "downgrade later phases to SKIP because render was NOT_RUN" block at
        the end; `active` is non-empty on every reachable path (`run`
        defaults it to `ALL_PHASES`, and the request models reject an empty
        or unknown set), so both were dead and both said the opposite of the
        documented rule.
        """
        reset_case_output_directory(
            self.output_root,
            chart=cfg.row.chart,
            environment=cfg.row.env,
        )
        render_result = self._timed(
            cfg.row,
            "render",
            lambda: phases.render(
                cfg.row,
                helm=self.helm,
                chart_path=cfg.chart_path,
                values=cfg.values,
                output_root=self.output_root,
            ),
        )

        validator_results: dict[str, PhaseResult] = {}
        if render_result.status != "PASS":
            schema_result = blocked_phase_result(
                cfg,
                active,
                "schema",
                upstream="render",
            )
            policy_result = blocked_phase_result(
                cfg,
                active,
                "policy",
                upstream="render",
            )
            validator_results.update(
                self._blocked_validator_results(
                    cfg,
                    ValidatorCategory.SCHEMA,
                    schema_result,
                )
            )
            validator_results.update(
                self._blocked_validator_results(
                    cfg,
                    ValidatorCategory.POLICY,
                    policy_result,
                )
            )
        else:
            rendered_dir = (
                render_result.artifacts[0]
                if render_result.artifacts
                else (self.output_root / cfg.row.chart / cfg.row.env)
            )
            schema_result, schema_validators = self._run_category(
                cfg,
                ValidatorCategory.SCHEMA,
                rendered_dir,
                active,
            )
            validator_results.update(schema_validators)
            if schema_result.status == "FAIL":
                policy_result = blocked_phase_result(
                    cfg,
                    active,
                    "policy",
                    upstream="schema",
                )
                validator_results.update(
                    self._blocked_validator_results(
                        cfg,
                        ValidatorCategory.POLICY,
                        policy_result,
                    )
                )
            else:
                policy_result, policy_validators = self._run_category(
                    cfg,
                    ValidatorCategory.POLICY,
                    rendered_dir,
                    active,
                )
                validator_results.update(policy_validators)

        phase_map: dict[str, PhaseResult] = {
            "render": render_result,
            "schema": schema_result,
            "policy": policy_result,
        }
        return RowResult(
            row=cfg.row,
            phases=phase_map,
            validator_results=validator_results,
        )

    def _run_category(
        self,
        cfg: RowConfig,
        category: ValidatorCategory,
        rendered_dir: Path,
        active: frozenset[str],
    ) -> tuple[PhaseResult, dict[str, PhaseResult]]:
        """Run enabled validators in one stable gate and aggregate their result."""
        invocations = cfg.invocations_for(category)
        phase: PhaseName = (
            "schema" if category is ValidatorCategory.SCHEMA else "policy"
        )
        if category.value not in active:
            result = PhaseResult(phase=phase, status="NOT_RUN")
            return result, {
                invocation.validator_id: result for invocation in invocations
            }

        enabled = tuple(invocation for invocation in invocations if invocation.enabled)
        disabled_result = PhaseResult(
            phase=phase,
            status="SKIP",
            detail="disabled by chart-lifecycle",
            skip_cause="validator_disabled",
        )
        results: dict[str, PhaseResult] = {
            invocation.validator_id: disabled_result
            for invocation in invocations
            if not invocation.enabled
        }
        if not enabled:
            return disabled_result, results

        self._emit(cfg.row, category.value, "running", None)
        start = time.monotonic()
        for invocation in enabled:
            executor = self.validators.get(invocation.validator_id)
            if executor is None:
                results[invocation.validator_id] = PhaseResult(
                    phase=phase,
                    status="FAIL",
                    detail=f"validator executor unavailable: {invocation.validator_id}",
                    error_type="tool",
                )
                continue
            results[invocation.validator_id] = executor.validate(
                rendered_dir,
                invocation.config,
            )
        elapsed = time.monotonic() - start
        aggregate = self._aggregate_category(category, enabled, results)
        aggregate = PhaseResult(
            phase=aggregate.phase,
            status=aggregate.status,
            detail=aggregate.detail,
            artifacts=aggregate.artifacts,
            error_type=aggregate.error_type,
            skip_cause=aggregate.skip_cause,
            elapsed_seconds=elapsed,
        )
        self._emit(cfg.row, category.value, aggregate.status, elapsed)
        if len(enabled) == 1:
            results[enabled[0].validator_id] = aggregate
        return aggregate, results

    @staticmethod
    def _aggregate_category(
        category: ValidatorCategory,
        enabled: tuple[ValidatorInvocation, ...],
        results: dict[str, PhaseResult],
    ) -> PhaseResult:
        """Fold concrete validator outcomes into the stable category phase."""
        selected = tuple(
            (invocation.validator_id, results[invocation.validator_id])
            for invocation in enabled
        )
        if len(selected) == 1:
            return selected[0][1]

        statuses = {result.status for _, result in selected}
        status: PhaseStatus = (
            "FAIL"
            if "FAIL" in statuses
            else "PASS"
            if "PASS" in statuses
            else "SKIP"
        )
        error_type: ErrorType | None = (
            "spec"
            if any(result.error_type == "spec" for _, result in selected)
            else "tool"
            if any(result.error_type == "tool" for _, result in selected)
            else None
        )
        details = [
            f"[{validator_id}] {result.detail}"
            for validator_id, result in selected
            if result.detail
        ]
        artifacts = tuple(
            artifact
            for _, result in selected
            for artifact in result.artifacts
        )
        return PhaseResult(
            phase=category.value,
            status=status,
            detail="\n".join(details) or None,
            artifacts=artifacts,
            error_type=error_type,
        )

    @staticmethod
    def _blocked_validator_results(
        cfg: RowConfig,
        category: ValidatorCategory,
        aggregate: PhaseResult,
    ) -> dict[str, PhaseResult]:
        """Project a blocked/disabled category result onto its identities."""
        return {
            invocation.validator_id: (
                aggregate
                if invocation.enabled
                else PhaseResult(
                    phase=category.value,
                    status="SKIP",
                    detail="disabled by chart-lifecycle",
                    skip_cause="validator_disabled",
                )
            )
            for invocation in cfg.invocations_for(category)
        }

    def _timed(
        self,
        row: WorklistRow,
        name: str,
        fn: Callable[[], PhaseResult],
    ) -> PhaseResult:
        """Run a phase fn, stamp elapsed_seconds, and emit start/end events."""
        self._emit(row, name, "running", None)
        start = time.monotonic()
        try:
            result = fn()
        finally:
            elapsed = time.monotonic() - start
        result = PhaseResult(
            phase=result.phase,
            status=result.status,
            detail=result.detail,
            artifacts=result.artifacts,
            error_type=result.error_type,
            skip_cause=result.skip_cause,
            elapsed_seconds=elapsed,
        )
        self._emit(row, name, result.status, elapsed)
        return result

    def _crash_row(
        self,
        cfg: RowConfig,
        exc: Exception,
        *,
        active: frozenset[str],
        context: str = "worker crashed",
    ) -> RowResult:
        """Convert a worker-level crash into a visible row failure.

        error_type="tool" routes to `Outcome.TOOL` (a tool/runtime fault, not
        a chart-author validation issue). Schema/policy SKIP downstream so
        the row reads consistently with an in-phase render FAIL.
        """
        tb = traceback.format_exception_only(type(exc), exc)
        detail = (tb[-1] if tb else repr(exc)).strip()
        render = PhaseResult(
            phase="render",
            status="FAIL",
            detail=f"{context}: {detail}",
            error_type="tool",
        )
        schema = blocked_phase_result(cfg, active, "schema", upstream="render")
        policy = blocked_phase_result(cfg, active, "policy", upstream="render")
        return RowResult(
            row=cfg.row,
            phases={"render": render, "schema": schema, "policy": policy},
        )

    @staticmethod
    def _row_failed(result: RowResult) -> bool:
        """Return whether a row contains any failed phase."""
        return any(phase.status == "FAIL" for phase in result.phases.values())

    @staticmethod
    def _not_run_row(cfg: RowConfig) -> RowResult:
        """Represent a row deliberately omitted after a fail-fast failure."""
        phases = {
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
        }
        return RowResult(row=cfg.row, phases=phases)

    def _finalize_row(self, result: RowResult) -> None:
        """Emit exactly one terminal event for every phase result."""
        for phase in result.phases.values():
            self._emit(
                result.row,
                phase.phase,
                phase.status,
                phase.elapsed_seconds,
            )

    def _emit(
        self,
        row: WorklistRow,
        phase: str,
        status: str,
        elapsed_s: float | None,
    ) -> None:
        """Emit an event, suppressing duplicate terminal notifications."""
        if status in {"PASS", "FAIL", "SKIP", "NOT_RUN"}:
            key = (row, phase)
            with self._event_lock:
                if key in self._terminal_events:
                    return
                self._terminal_events.add(key)
        self.on_event(row, phase, status, elapsed_s)
