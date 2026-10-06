"""Wire contracts for `chart list` and `chart show`.

This module is the single source of truth for the machine-readable shapes
those two commands emit. Every surface -- the CLI's `-o json|yaml`, a REST
endpoint, a Slack app -- projects through these functions, so a second
surface cannot answer "what charts are there?" with a different document.

`lifecycle_to_dict` returns the authored `ChartLifecycle` envelope itself,
so "what did I author / what did the tool normalize it to" is a plain diff.

Deliberately I/O-free and format-free, matching the other wire modules:
these functions return plain dicts. Choosing an encoder (`json.dumps`
indentation, YAML, an HTTP body) and writing it is the surface's job.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from chart_manager.api.v1alpha1.chart_lifecycle import ChartLifecycle
from chart_manager.commands.catalog.run import ChartCatalogEntry

__all__ = ["catalog_to_dict", "lifecycle_to_dict"]


def catalog_to_dict(entries: Sequence[ChartCatalogEntry]) -> dict[str, Any]:
    """Project the chart catalog onto the `chart list` payload."""
    return {
        "charts": [_entry_to_dict(entry) for entry in entries],
    }


def _entry_to_dict(entry: ChartCatalogEntry) -> dict[str, Any]:
    """Project one catalog entry; `error` is always present, null when clean.

    A stable key set rather than the `exclude_none` shape `chart show` uses:
    this document is a *list*, and the question asked of it most often is
    "which charts are broken?". `jq '.charts[] | select(.error)'` must work
    without the caller first knowing whether the key exists.
    """
    return {
        "name": entry.name,
        "type": entry.chart_type,
        "version": entry.version,
        "dependencies": list(entry.dependencies),
        "lifecycle": entry.lifecycle_status,
        "manifest_validation": entry.validation.value,
        "chart_test": entry.chart_test.value,
        "profiles": list(entry.profiles),
        "error": entry.error,
    }


def lifecycle_to_dict(lifecycle: ChartLifecycle) -> dict[str, Any]:
    """Project one chart's normalized lifecycle intent.

    `by_alias` so the keys are the ones a chart author typed (`apiVersion`,
    `chartTest`), not the snake_case field names pydantic binds them to --
    the point of `chart show` is to hand back the authored document after
    normalization, and a reader must be able to paste it into
    `chart-lifecycle.yaml`. `exclude_none` for the same reason: an unset
    optional section was never in the authored file, and echoing it back as
    `null` would suggest it is a thing to configure.
    """
    return lifecycle.model_dump(mode="json", by_alias=True, exclude_none=True)
