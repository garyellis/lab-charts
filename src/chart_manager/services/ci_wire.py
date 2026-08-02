"""Wire contract for the GitHub Actions cluster-test matrix.

This module is the single source of truth for the machine-readable shape CI
consumes. `.github/workflows/ci.yaml` captures the emitting command's stdout
into a shell variable and feeds it to `strategy.matrix`, so the payload's
shape is an external contract with GitHub Actions, not an internal detail.
Built in the CLI it was invisible to `services/`; a REST or Slack surface
that wanted to hand the same matrix to a workflow would have had to copy the
dict literal out of `cli/main.py`.

**Editing this module is a breaking change.** GitHub's `matrix.include` shape
is fixed by GitHub, not by us -- which is why there is no `SCHEMA_VERSION`
here, unlike `services/helmrelease/wire.py`. Adding a key to an entry is
additive and safe (it becomes another `matrix.<key>` in the workflow);
renaming or removing `chart`/`profile` breaks every job that references
`matrix.chart` or `matrix.profile`.

Deliberately I/O-free and format-free, matching the other wire modules:
these functions return plain dicts. Choosing an encoder -- `json.dumps`
separators, YAML, an HTTP response body -- and performing the write is the
surface's job.

The *selection* of which entries belong in the matrix is a separate concern
and lives in `services/ci.py` (`MatrixSelection`, `CiService.matrix`). This
module only shapes what selection returned.
"""

from __future__ import annotations

from collections.abc import Sequence

from chart_manager.services.lifecycle.impact import ClusterTestImpact

__all__ = ["cluster_test_matrix_to_dict"]


def cluster_test_matrix_to_dict(
    entries: Sequence[ClusterTestImpact],
) -> dict[str, list[dict[str, str]]]:
    """Project selected matrix entries onto the GitHub Actions matrix payload.

    Deliberately narrower than `lifecycle/wire.py`'s `impact_to_dict`: the selection
    `reasons` are why a chart was chosen, which is useful to a human reading
    `ci impact` and meaningless to `strategy.matrix`. Including them would
    add a `matrix.reasons` dimension to every job.
    """
    return {
        "include": [
            {"chart": entry.chart, "profile": entry.profile} for entry in entries
        ]
    }
