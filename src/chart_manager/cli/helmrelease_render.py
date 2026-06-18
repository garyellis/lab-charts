"""Pretty/JSON renderers and live progress driver for `helmrelease monitor/test`.

Module-level functions, no Renderer protocol/ABC -- the CLI handler picks one
of four functions based on (command, mode). _PrettyProgressDriver is the
only stateful piece, used as a context manager during pretty runs to hold a
Rich Live table; thread-safe under the monitor/test executor.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import IO, Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from chart_manager.integrations.flux import HelmReleaseRef
from chart_manager.services.helmrelease import (
    NO_MATCH_REF,
    MonitorOutcome,
    MonitorResult,
    TestOutcome,
    TestResult,
    Transition,
)

_LOG = logging.getLogger(__name__)
_SCHEMA_VERSION = 1


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{sec:02d}s"


def _serialize_condition(c: Any) -> dict[str, Any]:
    return {
        "type": c.type,
        "status": c.status,
        "reason": c.reason,
        "message": c.message,
        "last_transition_time": (
            c.last_transition_time.isoformat() if c.last_transition_time else None
        ),
    }


def _serialize_transition(t: Transition) -> dict[str, Any]:
    return {
        "at": t.at.isoformat() if isinstance(t.at, datetime) else str(t.at),
        "phase": t.phase,
        "detail": t.detail,
    }


def _serialize_workload_rollout(w: Any) -> dict[str, Any]:
    wl = w.workload
    return {
        "kind": wl.kind,
        "namespace": wl.namespace,
        "name": wl.name,
        "desired": wl.desired,
        "ready": wl.ready,
        "available": wl.available,
        "generation": w.generation,
        "observed_generation": w.observed_generation,
        "converged": w.converged,
    }


def _serialize_monitor_outcome(o: MonitorOutcome) -> dict[str, Any]:
    status = o.last_status
    return {
        "namespace": o.ref.namespace,
        "name": o.ref.name,
        "verdict": o.verdict,
        "reason": o.reason,
        "duration_seconds": o.duration_seconds,
        "conditions": (
            [_serialize_condition(c) for c in status.conditions] if status else []
        ),
        "observed_generation": status.observed_generation if status else None,
        "generation": status.generation if status else None,
        "history_chart_version": status.history_chart_version if status else None,
        "workloads": [_serialize_workload_rollout(w) for w in o.last_workloads],
        "recent_transitions": [_serialize_transition(t) for t in o.recent_transitions],
        "diagnostics": o.diagnostics,
    }


def _serialize_test_outcome(o: TestOutcome) -> dict[str, Any]:
    return {
        "namespace": o.ref.namespace,
        "name": o.ref.name,
        "verdict": o.verdict,
        "reason": o.reason,
        "duration_seconds": o.duration_seconds,
        "helm_test_returncode": o.helm_test_returncode,
        "helm_test_stdout": o.helm_test_stdout,
        "helm_test_stderr": o.helm_test_stderr,
        "test_pods": [
            {
                "namespace": p.namespace,
                "name": p.name,
                "phase": p.phase,
                "logs": p.logs,
                "previous_logs": p.previous_logs,
            }
            for p in o.test_pods
        ],
        "phase_log": [_serialize_transition(t) for t in o.phase_log],
        "diagnostics": o.diagnostics,
    }


def _summary_line(*, ok_count: int, total: int, duration: float) -> str:
    return f"{ok_count}/{total} ready in {_fmt_duration(duration)}"


def render_monitor_pretty(
    result: MonitorResult,
    console: Console,
    *,
    chart: str,
    version: str,
) -> None:
    real_outcomes = tuple(o for o in result.outcomes if o.ref is not NO_MATCH_REF)
    if not real_outcomes:
        console.print(
            f"[yellow]no helmreleases matched[/yellow] chart={chart} version={version}"
        )
        return

    ok_count = sum(1 for o in real_outcomes if o.verdict in ("ready", "skipped-suspended"))
    summary = _summary_line(
        ok_count=ok_count, total=len(real_outcomes), duration=result.total_duration_seconds
    )
    headline_style = "green" if result.ok else "red"
    console.print(f"[{headline_style}]{summary}[/{headline_style}]  chart={chart}@{version}")

    table = Table("Namespace", "Name", "Verdict", "Duration", "Ready Reason")
    for o in real_outcomes:
        ready_reason = ""
        if o.last_status and o.last_status.ready:
            ready_reason = o.last_status.ready.reason
        style = _verdict_style(o.verdict)
        table.add_row(
            o.ref.namespace,
            o.ref.name,
            f"[{style}]{o.verdict}[/{style}]",
            _fmt_duration(o.duration_seconds),
            ready_reason,
        )
    console.print(table)

    for o in real_outcomes:
        if o.verdict in ("ready", "skipped-suspended"):
            continue
        if not o.diagnostics:
            continue
        recent = o.recent_transitions[-3:]
        body = o.diagnostics
        if recent:
            body += "\n\n--- last transitions ---\n"
            body += "\n".join(
                f"{t.at.isoformat()} {t.phase} - {t.detail}" for t in recent
            )
        console.print(
            Panel(body, title=f"{o.ref.namespace}/{o.ref.name} [{o.verdict}]", border_style="red")
        )


def render_monitor_json(
    result: MonitorResult,
    file: IO[str],
    *,
    chart: str,
    version: str,
) -> None:
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "command": "monitor",
        "chart": chart,
        "version": version,
        "ok": result.ok,
        "total_timed_out": result.total_timed_out,
        "duration_seconds": result.total_duration_seconds,
        "outcomes": [_serialize_monitor_outcome(o) for o in result.outcomes],
    }
    json.dump(payload, file, sort_keys=True, separators=(",", ":"), default=str)
    file.write("\n")
    file.flush()


def render_test_pretty(
    result: TestResult,
    console: Console,
    *,
    chart: str,
    version: str,
) -> None:
    real_outcomes = tuple(o for o in result.outcomes if o.ref is not NO_MATCH_REF)
    if not real_outcomes:
        console.print(
            f"[yellow]no helmreleases matched[/yellow] chart={chart} version={version}"
        )
        return

    ok_count = sum(1 for o in real_outcomes if o.verdict in ("passed", "skipped-suspended"))
    headline_style = "green" if result.ok else "red"
    summary = (
        f"{ok_count}/{len(real_outcomes)} passed in "
        f"{_fmt_duration(result.total_duration_seconds)}"
    )
    console.print(f"[{headline_style}]{summary}[/{headline_style}]  chart={chart}@{version}")

    table = Table("Namespace", "Name", "Verdict", "Duration", "Reason")
    for o in real_outcomes:
        style = _verdict_style(o.verdict)
        table.add_row(
            o.ref.namespace,
            o.ref.name,
            f"[{style}]{o.verdict}[/{style}]",
            _fmt_duration(o.duration_seconds),
            o.reason,
        )
    console.print(table)

    for o in real_outcomes:
        if o.verdict in ("passed", "skipped-suspended"):
            continue
        if not o.diagnostics:
            continue
        recent = o.phase_log[-3:]
        body = o.diagnostics
        if recent:
            body += "\n\n--- phase log ---\n"
            body += "\n".join(
                f"{t.at.isoformat()} {t.phase} - {t.detail}" for t in recent
            )
        console.print(
            Panel(body, title=f"{o.ref.namespace}/{o.ref.name} [{o.verdict}]", border_style="red")
        )


def render_test_json(
    result: TestResult,
    file: IO[str],
    *,
    chart: str,
    version: str,
) -> None:
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "command": "test",
        "chart": chart,
        "version": version,
        "ok": result.ok,
        "total_timed_out": result.total_timed_out,
        "duration_seconds": result.total_duration_seconds,
        "outcomes": [_serialize_test_outcome(o) for o in result.outcomes],
    }
    json.dump(payload, file, sort_keys=True, separators=(",", ":"), default=str)
    file.write("\n")
    file.flush()


def _verdict_style(verdict: str) -> str:
    if verdict in ("ready", "passed"):
        return "green"
    if verdict in ("skipped-suspended", "skipped-not-ready"):
        return "yellow"
    if verdict == "no-match":
        return "yellow"
    return "red"


class _PrettyProgressDriver:
    """Thread-safe live progress driver. Used as a context manager.

    Holds a Rich Live table that re-renders per-HR transitions. The lock
    guards both the per-HR state map and the Live.update call so concurrent
    worker threads cannot interleave updates. Render exceptions are caught
    and logged -- a render bug must never break the underlying run.
    """

    def __init__(self, console: Console, *, is_test: bool) -> None:
        self._console = console
        self._is_test = is_test
        self._lock = threading.Lock()
        self._state: dict[tuple[str, str], Transition] = {}
        # Lazy-imported so importing helmrelease_render in non-pretty paths
        # (CI logs, tests) doesn't drag rich.live into the process.
        from rich.live import Live

        self._Live = Live
        self._live: Any | None = None

    def __enter__(self) -> _PrettyProgressDriver:
        self._live = self._Live(
            self._render(),
            console=self._console,
            auto_refresh=True,
            refresh_per_second=4,
        )
        self._live.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._live is not None:
            self._live.__exit__(*exc)
            self._live = None

    def __call__(self, ref: HelmReleaseRef, transition: Transition) -> None:
        try:
            with self._lock:
                self._state[(ref.namespace, ref.name)] = transition
                if self._live is not None:
                    self._live.update(self._render())
        except Exception:
            _LOG.exception("progress driver update failed")

    def _render(self) -> Table:
        table = Table("Namespace", "Name", "Phase", "Detail")
        for (ns, name) in sorted(self._state.keys()):
            t = self._state[(ns, name)]
            table.add_row(ns, name, t.phase, t.detail)
        return table
