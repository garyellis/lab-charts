"""Validate pipeline runner.

Sequences render -> schema -> policy per row. Strict per-row dependency:
a render FAIL short-circuits both downstream phases to SKIP; a schema FAIL
short-circuits policy to SKIP. Across rows the runner is fail-fast: false
— every row is attempted so the operator sees the full failure surface in
one go.

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

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.integrations.kyverno import Kyverno
from chart_manager.plumbing.validate_models import (
    PhaseResult,
    RowResult,
    RunResult,
    WorklistRow,
)
from chart_manager.services.validate import phases

EventCallback = Callable[[WorklistRow, str, str, float | None], None]


@dataclass(frozen=True)
class RowConfig:
    """Per-row inputs threaded through every phase.

    M2 carried `RenderInputs` + `SchemaInputs`; M3 collapses to a single
    config so policy (and future phases) don't multiply the constructor
    surface. CLI builds these from flags; M4 will build them from
    `validate-spec.yaml`. `None` means use phase defaults; an empty list
    in `policy_paths` is a deliberate signal that no policies were
    discovered (phase => SKIP).
    """

    row: WorklistRow
    chart_path: Path
    values: list[Path] = field(default_factory=list)
    kubernetes_version: str | None = None
    schema_locations: list[str] | None = None
    policy_paths: list[Path] | None = None


class ValidateRunner:
    def __init__(
        self,
        *,
        helm: Helm,
        output_root: Path,
        kubeconform: Kubeconform | None = None,
        kyverno: Kyverno | None = None,
        max_workers: int = 1,
        on_event: EventCallback | None = None,
        dep_update_timeout: float | None = 300.0,
        row_timeout: float | None = None,
    ) -> None:
        self.helm = helm
        self.kubeconform = kubeconform or Kubeconform()
        self.kyverno = kyverno or Kyverno()
        self.output_root = output_root.resolve()
        self.max_workers = max(1, max_workers)
        # No-op default so phase code can fire-and-forget without a None
        # check on every event. CLI wires in a real callback.
        self.on_event: EventCallback = on_event or (lambda *_args: None)
        # 5-min default guards prefetch against hung OCI/DNS lookups. The
        # CLI exposes --dep-update-timeout for override (None = unbounded).
        self.dep_update_timeout = dep_update_timeout
        # Per-row hard cap on total wall-clock for ALL phases in one row.
        # Default None preserves legacy behavior; CLI exposes --row-timeout.
        # On timeout, the row is marked FAIL with error_type=tool.
        self.row_timeout = row_timeout

    def run(
        self,
        configs: list[RowConfig],
        *,
        enabled_phases: frozenset[str] | None = None,
    ) -> RunResult:
        """Execute render -> schema -> policy across rows.

        `enabled_phases` (default: all three) restricts which phases run;
        disabled phases get a `NOT_RUN` PhaseResult instead. Disabling a
        phase does NOT short-circuit later phases — schema-only runs still
        render first because schema needs the rendered tree.
        """
        if not configs:
            return RunResult(rows=(), rendered_root=self.output_root)

        active = enabled_phases if enabled_phases is not None else frozenset(
            {"render", "schema", "policy"}
        )

        # Propagate row_timeout onto each integration's per-subprocess cap.
        # The integrations honor `self.timeout` on their runner.run calls.
        # Done here (not in __init__) so swapping --row-timeout between runs
        # of a long-lived runner takes effect.
        if self.row_timeout is not None:
            self.helm.timeout = self.row_timeout
            self.kubeconform.timeout = self.row_timeout
            self.kyverno.timeout = self.row_timeout

        # Pre-fetch helm dependencies once per distinct chart before fanning
        # out per-row work. Without this, N envs of the same chart all hit
        # Helm.dependency_update serialized on the per-chart lock — the
        # first call holds the lock for the entire network fetch while the
        # other N-1 wait. Pre-fetching collapses that wait to a single up-
        # front pass that can itself parallelize across DISTINCT charts.
        if "render" in active:
            self._prefetch_dependencies(configs)

        if self.max_workers == 1:
            results = [self._run_row(cfg, active) for cfg in configs]
        else:
            results = []
            with ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="validate-",
            ) as pool:
                futures = {pool.submit(self._run_row, cfg, active): cfg for cfg in configs}
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
                        results.append(self._crash_row(cfg, exc))

        # Deterministic output order regardless of completion order.
        results.sort(key=lambda r: (r.row.chart, r.row.env))
        return RunResult(rows=tuple(results), rendered_root=self.output_root)

    def _prefetch_dependencies(self, configs: list[RowConfig]) -> None:
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
            return

        def _update(chart_path: Path) -> None:
            self.helm.dependency_update(chart_path, timeout=self.dep_update_timeout)

        if self.max_workers == 1 or len(distinct_charts) == 1:
            for chart_path in distinct_charts:
                _update(chart_path)
            return
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(distinct_charts)),
            thread_name_prefix="validate-deps-",
        ) as pool:
            list(pool.map(_update, distinct_charts))

    def _run_row(self, cfg: RowConfig, active: frozenset[str]) -> RowResult:
        if "render" in active or "schema" in active or "policy" in active:
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
        else:
            render_result = PhaseResult(phase="render", status="NOT_RUN")

        if render_result.status not in ("PASS", "NOT_RUN"):
            schema_result = PhaseResult(
                phase="schema",
                status="SKIP",
                detail="upstream render FAIL",
            )
            policy_result = PhaseResult(
                phase="policy",
                status="SKIP",
                detail="upstream render FAIL",
            )
        else:
            rendered_dir = (
                render_result.artifacts[0]
                if render_result.artifacts
                else (self.output_root / cfg.row.chart / cfg.row.env)
            )
            if "schema" in active:
                schema_result = self._timed(
                    cfg.row,
                    "schema",
                    lambda: phases.schema(
                        cfg.row,
                        kubeconform=self.kubeconform,
                        rendered_dir=rendered_dir,
                        kubernetes_version=cfg.kubernetes_version,
                        schema_locations=cfg.schema_locations,
                    ),
                )
            else:
                schema_result = PhaseResult(phase="schema", status="NOT_RUN")
            if schema_result.status == "FAIL":
                policy_result = PhaseResult(
                    phase="policy",
                    status="SKIP",
                    detail="upstream schema FAIL",
                )
            elif "policy" in active:
                policy_result = self._timed(
                    cfg.row,
                    "policy",
                    lambda: phases.policy(
                        cfg.row,
                        kyverno=self.kyverno,
                        rendered_dir=rendered_dir,
                        policy_paths=cfg.policy_paths or [],
                    ),
                )
            else:
                policy_result = PhaseResult(phase="policy", status="NOT_RUN")

        # If render was NOT_RUN but a later phase is active, downgrade
        # the later phase to SKIP — it has no manifests to chew on.
        if render_result.status == "NOT_RUN":
            if schema_result.status not in ("NOT_RUN",):
                schema_result = PhaseResult(
                    phase="schema",
                    status="SKIP",
                    detail="render not run",
                )
            if policy_result.status not in ("NOT_RUN",):
                policy_result = PhaseResult(
                    phase="policy",
                    status="SKIP",
                    detail="render not run",
                )

        phase_map: dict[str, PhaseResult] = {
            "render": render_result,
            "schema": schema_result,
            "policy": policy_result,
        }
        return RowResult(row=cfg.row, phases=phase_map)

    def _timed(
        self,
        row: WorklistRow,
        name: str,
        fn: Callable[[], PhaseResult],
    ) -> PhaseResult:
        """Run a phase fn, stamp elapsed_seconds, and emit start/end events."""
        self.on_event(row, name, "running", None)
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
            elapsed_seconds=elapsed,
        )
        self.on_event(row, name, result.status, elapsed)
        return result

    def _crash_row(self, cfg: RowConfig, exc: Exception) -> RowResult:
        """Convert a worker-level crash into a visible row failure.

        error_type="tool" routes to exit code 2 (a tool/runtime fault, not
        a chart-author validation issue). Schema/policy SKIP downstream so
        the row reads consistently with an in-phase render FAIL.
        """
        tb = traceback.format_exception_only(type(exc), exc)
        detail = (tb[-1] if tb else repr(exc)).strip()
        render = PhaseResult(
            phase="render",
            status="FAIL",
            detail=f"worker crashed: {detail}",
            error_type="tool",
        )
        schema = PhaseResult(
            phase="schema", status="SKIP", detail="upstream render FAIL"
        )
        policy = PhaseResult(
            phase="policy", status="SKIP", detail="upstream render FAIL"
        )
        # Emit events for ALL three phases so progress displays stay in
        # sync. PlainNarrationDisplay's [done/total] counter bumps on the
        # 'policy' end event; without these, a crashed row would leave the
        # counter short by one for the rest of the run.
        self.on_event(cfg.row, "render", "FAIL", None)
        self.on_event(cfg.row, "schema", "SKIP", None)
        self.on_event(cfg.row, "policy", "SKIP", None)
        return RowResult(
            row=cfg.row,
            phases={"render": render, "schema": schema, "policy": policy},
        )
