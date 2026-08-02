"""Manifest-validation pipeline runner.

Sequences render -> schema -> policy per row. Strict per-row dependency:
a render FAIL short-circuits both downstream phases to SKIP; a schema FAIL
short-circuits policy to SKIP. Across rows the default is fail-fast false,
so every independent row is attempted and the operator sees the full failure
surface in one run. Callers may opt into truthful fail-fast execution, which
stops before later rows start.

Rows are independent: with max_workers > 1 they execute concurrently via a
ThreadPoolExecutor. The per-row sequencing above is preserved inside each
worker. The validators (and the integrations they call) are responsible
for their own thread-safety; Helm's per-chart `dependency update` dedupe
is the load-bearing example.

A row carries its own helm binding and the runner memoizes one `Helm` per
distinct binding through `helm_factory`. That is what lets ONE runner see
every row of a run: binding a single `Helm` to the runner instead forced the
caller to partition rows by binding and re-implement fan-out, fail-fast,
NOT_RUN synthesis, crash rows, ordering and event dedupe one layer up -- and
made rows under different bindings run strictly group-after-group, never
concurrently. `crash_row` and `not_run_row` are module-level for the same
reason: the one caller that still synthesizes rows outside a run (the
service, when the runner itself cannot be constructed) uses these rather
than a second copy.

Render lives here as `_render` rather than in a `phases` module of its own:
it is not a pluggable gate the way schema and policy are, it has exactly one
caller, and the tree it writes is the input every gate reads.
"""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.errors import ExternalCommandError, SpecError
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
from chart_manager.services.manifest_validation.paths import (
    case_output_directory,
    reset_case_output_directory,
)
from chart_manager.services.manifest_validation.validators import (
    ManifestValidator,
    ValidatorCategory,
    ValidatorInvocation,
)

#: The run's diagnostic channel, parallel to `on_event`. `on_event` is
#: presentation -- optional, suppressed by `-o json`, and carrying neither a
#: level nor a timestamp -- so it cannot answer "what happened?" after a CI
#: run failed. Everything this module converts into a row (a crashed worker, an
#: unusable helm binding, a failed prefetch, a helm render that died) is
#: recorded here as well, with the exception detail the row has no room for.
_LOG = logging.getLogger(__name__)

EventCallback = Callable[[WorklistRow, str, str, float | None], None]

#: A chart's authored helm selection: (pinned version, explicit binary). The
#: spec allows at most one of the two, so `(None, None)` is the ambient helm.
HelmBinding = tuple[str | None, str | None]

HelmFactory = Callable[[str | None, str | None], Helm]


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
    #: The chart's authored helm selection. Carried per row rather than per
    #: runner so one runner can execute a whole heterogeneous batch.
    helm_version: str | None = None
    helm_binary: str | None = None

    @property
    def helm_binding(self) -> HelmBinding:
        """The (version, binary) pair this row's helm invocations run under."""
        return (self.helm_version, self.helm_binary)

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


def crash_row(
    cfg: RowConfig,
    exc: Exception,
    *,
    active: frozenset[str],
    context: str = "worker crashed",
) -> RowResult:
    """Convert a failure outside any validation phase into a visible row failure.

    error_type="tool" routes to `Outcome.TOOL` (a tool/runtime fault, not a
    chart-author validation issue). Schema/policy SKIP downstream so the row
    reads consistently with an in-phase render FAIL. `context` names the
    boundary that failed -- a worker crash, a dependency prefetch, an
    unusable helm binding, or a runner that could not be constructed at all.
    """
    tb = traceback.format_exception_only(type(exc), exc)
    detail = (tb[-1] if tb else repr(exc)).strip()
    render = PhaseResult(
        phase="render",
        status="FAIL",
        detail=f"{context}: {detail}",
        error_type="tool",
    )
    return RowResult(
        row=cfg.row,
        phases={
            "render": render,
            "schema": blocked_phase_result(cfg, active, "schema", upstream="render"),
            "policy": blocked_phase_result(cfg, active, "policy", upstream="render"),
        },
    )


def not_run_row(cfg: RowConfig) -> RowResult:
    """Represent a row deliberately omitted after a fail-fast failure."""
    phases: tuple[PhaseName, ...] = ("render", "schema", "policy")
    return RowResult(
        row=cfg.row,
        phases={
            phase: PhaseResult(
                phase=phase,
                status="NOT_RUN",
                detail="fail-fast: earlier row failed",
            )
            for phase in phases
        },
    )


class ManifestValidationRunner:
    """Orchestrate render -> schema -> policy across worklist rows, optionally in parallel."""

    def __init__(
        self,
        *,
        helm_factory: HelmFactory,
        output_root: Path,
        validators: dict[str, ManifestValidator],
        max_workers: int = 1,
        on_event: EventCallback | None = None,
        dep_update_timeout: float | None = 300.0,
        tool_timeout: float | None = None,
    ) -> None:
        """Wire integrations, worker count, event callback, and dep/tool timeouts."""
        self.helm_factory = helm_factory
        self._helm_by_binding: dict[HelmBinding, Helm] = {}
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
        # Per-SUBPROCESS wall-clock cap, applied to each tool invocation a row
        # makes (helm template, then each validator), not to the row's total.
        # An N-phase row can therefore take up to N times this value. It was
        # spelled `row_timeout` and documented as a per-row cap it never
        # enforced; a real row deadline would have to reach a per-call timeout
        # argument through Helm.template, the ManifestValidator protocol, both
        # adapters, and both validator integrations, so the name moved to
        # match the behavior instead. Default None = unbounded.
        # On tool timeout, the row is marked FAIL with error_type=tool.
        # Stamped onto each Helm as it is built (see `_helm_for`), before any
        # fan-out: a Helm is shared across `max_workers` threads, so mutating
        # it after fan-out would race -- which is also why this cannot become
        # a per-row remaining budget without the per-call plumbing above.
        # Validator providers receive the same timeout while constructing
        # their executors in the service composition root.
        self.tool_timeout = tool_timeout

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
        started = time.monotonic()
        # The resolved parameters, not the requested ones: `max_workers` has
        # already been floored at 1 and fail-fast overrides it to serial below,
        # so an operator reading "workers=8" in a log next to a serial timeline
        # would be reading the request rather than the run.
        _LOG.info(
            "validate run started: rows=%d workers=%d fail_fast=%s phases=%s "
            "tool_timeout=%s dep_update_timeout=%s output_root=%s",
            len(configs),
            1 if fail_fast else self.max_workers,
            fail_fast,
            ",".join(sorted(active)),
            self.tool_timeout,
            self.dep_update_timeout,
            self.output_root,
        )

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

        with self._event_lock:
            self._terminal_events.clear()

        blockers = self._prepare(configs, active=active, fail_fast=fail_fast)
        results: list[RowResult] = []
        runnable: list[RowConfig] = []
        stopped = False
        for cfg in configs:
            blocked = blockers.get((cfg.helm_binding, cfg.chart_path.resolve()))
            if stopped:
                results.append(not_run_row(cfg))
            elif blocked is not None:
                exc, context = blocked
                results.append(crash_row(cfg, exc, active=active, context=context))
                stopped = fail_fast
            else:
                runnable.append(cfg)

        # Fail-fast execution is intentionally serial. It gives the flag a
        # truthful boundary: once one row fails, no later row has already
        # started in another worker.
        if self.max_workers == 1 or fail_fast:
            for index, cfg in enumerate(runnable):
                if stopped:
                    results.append(not_run_row(cfg))
                    continue
                try:
                    row_result = self._run_row(cfg, active)
                except Exception as exc:
                    _LOG.exception(
                        "validate worker crashed: chart=%s env=%s",
                        cfg.row.chart,
                        cfg.row.env,
                    )
                    row_result = crash_row(cfg, exc, active=active)
                results.append(row_result)
                if fail_fast and self._row_failed(row_result):
                    stopped = True
                    for remaining in runnable[index + 1 :]:
                        results.append(not_run_row(remaining))
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
                        _LOG.exception(
                            "validate worker crashed: chart=%s env=%s",
                            cfg.row.chart,
                            cfg.row.env,
                        )
                        results.append(crash_row(cfg, exc, active=active))

        for result in results:
            self._finalize_row(result)

        # Deterministic output order regardless of completion order.
        results.sort(key=lambda r: (r.row.chart, r.row.env))
        failed = sum(1 for result in results if self._row_failed(result))
        _LOG.info(
            "validate run finished: rows=%d failed=%d not_run=%d elapsed=%.2fs",
            len(results),
            failed,
            sum(
                1
                for result in results
                if all(phase.status == "NOT_RUN" for phase in result.phases.values())
            ),
            time.monotonic() - started,
        )
        return RunResult(rows=tuple(results), rendered_root=self.output_root)

    # --- helm bindings -----------------------------------------------------

    def _helm_for(self, binding: HelmBinding) -> Helm:
        """Return the one Helm bound to this (version, binary) pair.

        Memoized because Helm's per-chart `dependency update` dedupe lives on
        the instance: two rows sharing a binding have to share the instance or
        they both fetch. Unsynchronized on purpose -- `_bind_helms` populates
        every binding in the batch before any fan-out, so workers only read.
        """
        helm = self._helm_by_binding.get(binding)
        if helm is None:
            version, binary = binding
            helm = self.helm_factory(version, binary)
            if self.tool_timeout is not None:
                helm.timeout = self.tool_timeout
            self._helm_by_binding[binding] = helm
        return helm

    def _prepare(
        self,
        configs: list[RowConfig],
        *,
        active: frozenset[str],
        fail_fast: bool,
    ) -> dict[tuple[HelmBinding, Path], tuple[Exception, str]]:
        """Build the helms and prefetch the deps; report what blocks which rows.

        Both steps run before any fan-out and both fail per (binding, chart
        path), so they share one result map keyed by exactly the pair a row
        would have executed under. Helm construction is eager and serial on
        purpose: resolving a pinned version can shell out, and the failure has
        to be attributable to the rows that asked for that binding rather than
        to whichever worker happened to touch it first.
        """
        blockers: dict[tuple[HelmBinding, Path], tuple[Exception, str]] = {}
        unusable: set[HelmBinding] = set()
        for binding in dict.fromkeys(cfg.helm_binding for cfg in configs):
            try:
                self._helm_for(binding)
            except Exception as exc:
                version, binary = binding
                _LOG.error(
                    "helm binding unavailable: version=%s binary=%s: %s: %s",
                    version or "(ambient)",
                    binary or "(ambient)",
                    type(exc).__name__,
                    exc,
                )
                unusable.add(binding)
                for cfg in configs:
                    if cfg.helm_binding == binding:
                        key = (binding, cfg.chart_path.resolve())
                        blockers[key] = (exc, "helm binding unavailable")
        # NOTE: `_run_row` renders unconditionally (schema and policy need the
        # tree), so this gate is narrower than the work it guards: a
        # `--phase schema` run renders WITHOUT the prefetch and each row pays
        # its own first-time dep fetch. Left as-is because widening it adds
        # `helm dependency update` subprocesses to a run that does not ask for
        # them today; fix it deliberately, with a timing test, not as a side
        # effect.
        if "render" in active:
            prefetched = self._prefetch_dependencies(
                configs,
                fail_fast=fail_fast,
                unusable=frozenset(unusable),
            )
            for key, failure in prefetched.items():
                _LOG.error(
                    "dependency prefetch failed: chart_path=%s: %s: %s",
                    key[1],
                    type(failure).__name__,
                    failure,
                )
                blockers[key] = (failure, "dependency prefetch failed")
        return blockers

    def _prefetch_dependencies(
        self,
        configs: list[RowConfig],
        *,
        fail_fast: bool,
        unusable: frozenset[HelmBinding] = frozenset(),
    ) -> dict[tuple[HelmBinding, Path], Exception]:
        """Run `helm dependency update` once per distinct (binding, chart path).

        Helm.dependency_update is already idempotent (per-chart lock + dedupe
        set), so this is technically redundant — but doing the prefetch BEFORE
        the worker fan-out means no row blocks on another row's first-time dep
        fetch. Parallelizes across distinct charts at the same worker count as
        the main pool: `Helm` locks per chart path, not per instance, so the
        fetches below genuinely overlap. `tests/test_manifest_validation_runner.py`
        asserts the wall-clock, because the claim is not checkable by reading
        this function alone.

        Keyed by binding as well as path because the dedupe set lives on the
        `Helm` instance: the same chart under two pinned helm versions is two
        fetches, and collapsing them would leave one of the two unfetched.
        `unusable` names bindings whose Helm could not be built at all; those
        rows already have a terminal result.
        """
        distinct: list[tuple[HelmBinding, Path]] = []
        seen: set[tuple[HelmBinding, Path]] = set()
        for cfg in configs:
            if cfg.helm_binding in unusable:
                continue
            key = (cfg.helm_binding, cfg.chart_path.resolve())
            if key in seen:
                continue
            seen.add(key)
            distinct.append((cfg.helm_binding, cfg.chart_path))

        if not distinct:
            return {}

        def _update(binding: HelmBinding, chart_path: Path) -> None:
            """Prefetch one chart's helm dependencies under one binding."""
            self._helm_for(binding).dependency_update(
                chart_path, timeout=self.dep_update_timeout
            )

        failures: dict[tuple[HelmBinding, Path], Exception] = {}
        if self.max_workers == 1 or len(distinct) == 1 or fail_fast:
            for binding, chart_path in distinct:
                try:
                    _update(binding, chart_path)
                except Exception as exc:
                    failures[(binding, chart_path.resolve())] = exc
                    if fail_fast:
                        break
            return failures
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(distinct)),
            thread_name_prefix="validate-deps-",
        ) as pool:
            futures = {
                pool.submit(_update, binding, path): (binding, path)
                for binding, path in distinct
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    binding, path = futures[future]
                    failures[(binding, path.resolve())] = exc
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
        render_result = self._timed(cfg.row, "render", lambda: self._render(cfg))

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
        # DEBUG, not INFO: one line per row per phase would bury the run
        # boundaries in a 200-row repository scan. The failing rows already
        # logged at ERROR from the phase that failed them.
        _LOG.debug(
            "validate row finished: chart=%s env=%s render=%s schema=%s policy=%s",
            cfg.row.chart,
            cfg.row.env,
            render_result.status,
            schema_result.status,
            policy_result.status,
        )
        return RowResult(
            row=cfg.row,
            phases=phase_map,
            validator_results=validator_results,
        )

    def _render(self, cfg: RowConfig) -> PhaseResult:
        """Render one (chart, env) into output_root/<chart>/<env>/.

        Output layout matches the worklist row keys so the schema and policy
        validators can locate manifests by row identity alone.

        This was the last resident of a `phases.py` that held one function
        with one caller. Render is not a pluggable gate the way schema and
        policy are -- every row renders, and the tree it produces is what the
        gates then read -- so it belongs to the thing that sequences rows.
        """
        out_dir = (self.output_root / cfg.row.chart / cfg.row.env).resolve()
        try:
            rendered = self._helm_for(cfg.helm_binding).template(
                cfg.row.release,
                cfg.chart_path,
                namespace=cfg.row.namespace,
                output_dir=out_dir,
                values=cfg.values,
            )
        except ExternalCommandError as exc:
            # Logged as well as returned: the PhaseResult reaches the summary
            # artifact, which a `--keep`-less failed run deletes, and reaches
            # nothing at all when the process dies before aggregating.
            _LOG.error(
                "helm template failed: chart=%s env=%s release=%s namespace=%s: %s",
                cfg.row.chart,
                cfg.row.env,
                cfg.row.release,
                cfg.row.namespace,
                exc,
            )
            # error_type="tool" promotes the row to `Outcome.TOOL` — the
            # underlying issue is a helm crash, not a chart-author problem.
            return PhaseResult(
                phase="render",
                status="FAIL",
                detail=str(exc),
                artifacts=(),
                error_type="tool",
            )

        return PhaseResult(
            phase="render",
            status="PASS",
            detail=None,
            artifacts=(rendered,),
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
                # A configuration fault, not a chart fault: the row fails for a
                # reason nothing in the chart can fix, so it needs to be
                # findable without reading the summary.
                _LOG.error(
                    "validator executor unavailable: validator=%s chart=%s env=%s",
                    invocation.validator_id,
                    cfg.row.chart,
                    cfg.row.env,
                )
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

    @staticmethod
    def _row_failed(result: RowResult) -> bool:
        """Return whether a row contains any failed phase."""
        return any(phase.status == "FAIL" for phase in result.phases.values())

    def _finalize_row(self, result: RowResult) -> None:
        """Emit exactly one terminal event for every phase result.

        `_emit` suppresses the phases a worker already narrated, so what this
        actually adds is the terminal event for phases nothing ran -- an
        upstream SKIP, a disabled NOT_RUN, a whole crash or fail-fast row.
        Together with `_emit` this is the run's ONLY event dedupe: a caller
        gets exactly one terminal event per (row, phase) and must not wrap a
        second one around it.
        """
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
