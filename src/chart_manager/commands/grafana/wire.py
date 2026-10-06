"""Wire contract for the Grafana dashboard lint report.

This module is the single source of truth for the machine-readable shape of a
lint run. Every surface -- `grafana dashboard lint -o json|yaml`, a future
REST endpoint, a CI annotation step -- projects through `lint_result_to_dict`
so they cannot diverge.

The payload carries the tally alongside the findings even though a consumer
could count the list, because `files_scanned` is not derivable from it: a run
over 40 dashboards that found nothing and a run over zero dashboards both
produce an empty `findings`, and those two are the case design doc 8.7 exists
to separate. `ok` is read off `LintResult.ok` rather than re-derived from the
list for the same reason the CLI does not re-derive it -- the pass/fail rule
belongs to `dashboard_lint`.

Deliberately I/O-free and format-free, matching the other wire modules: this
returns a plain, JSON-ready dict. Choosing an encoder and performing the
write is the surface's job -- see `commands/grafana/cli.py`.
"""

from __future__ import annotations

from typing import Any

from .dashboard_lint import LintResult

__all__ = [
    "lint_result_to_dict",
]


def lint_result_to_dict(result: LintResult) -> dict[str, Any]:
    """Project a `LintResult` onto the wire payload.

    Paths are emitted as POSIX strings so a report produced on one platform
    is comparable to one produced on another, matching
    `commands/upgrade/wire.py`.
    """
    return {
        "ok": result.ok,
        "files_scanned": result.files_scanned,
        "files_with_findings": result.files_with_findings,
        "findings": [
            {
                "path": finding.path.as_posix(),
                "rule": finding.rule,
                "message": finding.message,
            }
            for finding in result.findings
        ],
    }
