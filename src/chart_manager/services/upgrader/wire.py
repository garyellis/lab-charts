"""Versioned wire contract for `upgrade` and `upgrade-finalize`.

This module is the single source of truth for the machine-readable shape of an
upgrade outcome. Every surface -- the CLI's `--format json`, a REST endpoint, a
Slack app, a CI step -- projects through `upgrade_to_dict` / `finalize_to_dict`
so they cannot diverge while all claiming the same `SCHEMA_VERSION`.

**Editing this module is a breaking change.** Adding a key is additive and safe
at the current version; renaming, removing, or retyping a key requires bumping
`SCHEMA_VERSION`.

Both projections emit the *same* key set, because they describe the same event
from two angles: `UpgradeService` proposes a new wrapper-chart version and may
open a PR for it; `ChartFinalizer` is the Renovate callback that applies one.
The two result dataclasses spell the shared pair of versions differently --
`UpgradeResult.current_version`/`proposed_version` versus
`FinalizeResult.previous_version`/`version` -- so the mapping onto the contract
names (`current_wrapper_version`/`proposed_wrapper_version`) is made explicitly
here, once per dataclass. It is deliberately *not* a runtime key-fallback
chain: a surface that guesses which of three spellings a result object uses is
a surface that owns the contract.

Deliberately I/O-free and format-free: these functions return plain,
JSON-ready dicts. They take no `file`, no `format=`, no `console=`. Choosing an
encoder (`json.dumps` options, YAML, an HTTP response body) and rendering for
humans is the surface's job -- see `cli/upgrade.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .models import FinalizeResult, UpgradeResult

# Bump only on a breaking change to the payload shape; additive fields are
# safe at this version.
SCHEMA_VERSION = 1

__all__ = [
    "SCHEMA_VERSION",
    "finalize_to_dict",
    "upgrade_to_dict",
]


def upgrade_to_dict(result: UpgradeResult) -> dict[str, Any]:
    """Project an `UpgradeResult` onto the versioned wire payload."""
    return _payload(
        repository=result.repository,
        base=result.base,
        chart=result.chart,
        path=result.chart_path,
        current_wrapper_version=result.current_version,
        proposed_wrapper_version=result.proposed_version,
        branch=result.branch,
        outcome=result.outcome,
        pull_request=_pull_request(url=result.pr_url, number=result.pr_number),
        diagnostics=result.diagnostics,
    )


def finalize_to_dict(result: FinalizeResult, *, chart_path: Path) -> dict[str, Any]:
    """Project a `FinalizeResult` onto the versioned wire payload.

    The finalizer runs inside Renovate's callback on an already-checked-out
    branch: it resolves no repository, no base, and no branch, and it never
    opens a PR, so those keys are always null. It also carries no diagnostics
    channel of its own. `chart_path` is supplied by the caller because
    `FinalizeResult` does not carry the chart path it acted on.

    Unlike `UpgradeResult.proposed_version`, `FinalizeResult.version` is
    populated even when nothing changed (it then equals `previous_version`);
    `outcome` is the key that distinguishes the two cases.
    """
    return _payload(
        repository=None,
        base=None,
        chart=result.chart,
        path=chart_path,
        current_wrapper_version=result.previous_version,
        proposed_wrapper_version=result.version,
        branch=None,
        outcome="updated" if result.changed else "unchanged",
        pull_request=None,
        diagnostics=(),
    )


def _payload(
    *,
    repository: str | None,
    base: str | None,
    chart: str,
    path: Path,
    current_wrapper_version: str | None,
    proposed_wrapper_version: str | None,
    branch: str | None,
    outcome: str,
    pull_request: dict[str, Any] | None,
    diagnostics: Sequence[str],
) -> dict[str, Any]:
    """Assemble the one payload shape both projections must produce."""
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "base": base,
        "chart": chart,
        "path": path.as_posix(),
        "current_wrapper_version": current_wrapper_version,
        "proposed_wrapper_version": proposed_wrapper_version,
        "branch": branch,
        "outcome": outcome,
        "pull_request": pull_request,
        "diagnostics": list(diagnostics),
    }


def _pull_request(*, url: str | None, number: int | None) -> dict[str, Any] | None:
    """Nest the PR coordinates, or null when no PR exists.

    A half-populated result (a URL with no number, or the reverse) still yields
    an object: dropping it would report "no pull request" for a run that opened
    one.
    """
    if url is None and number is None:
        return None
    return {"url": url, "number": number}
