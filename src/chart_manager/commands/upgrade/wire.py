"""The machine-readable payload of `chart upgrade` and `upgrade-finalize`.

Both commands emit the same keys, mapped explicitly from each result type. These functions
return plain dicts; encoding and rendering belong to `cli.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .models import FinalizeResult, UpgradeResult

__all__ = [
    "finalize_to_dict",
    "upgrade_to_dict",
]


def upgrade_to_dict(result: UpgradeResult) -> dict[str, Any]:
    """Project an `UpgradeResult` onto the wire payload."""
    return _payload(
        repository=result.repository,
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
    """Project a `FinalizeResult` onto the wire payload.

    Finalize runs inside Renovate's callback, so repository, branch and pull request are null.
    `outcome` tells an unchanged run, whose version equals the previous one, from an updated one.
    """
    return _payload(
        repository=None,
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
        "repository": repository,
        "base": None,
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
    """Nest the PR coordinates, or null when there is neither a URL nor a number."""
    if url is None and number is None:
        return None
    return {"url": url, "number": number}
